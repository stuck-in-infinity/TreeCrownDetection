#!/usr/bin/env python3
"""Tree crown species classification pipeline.

Groups detected tree crowns by appearance and turns the groups a person names
into a species map. The stages are:

1. Crop each crown out of the orthomosaics using the polygon GeoJSONs
2. Extract DINOv2 features from the crops
3. Cluster the crowns at several values of k
4. Apply the species names the user filled in for each cluster
5. Score the result against ground truth, if there is any
6. Export a KMZ for Google Earth
"""

import os
import re
import shutil
import json
import zipfile
import warnings
import argparse
from pathlib import Path

import numpy as np
import pandas as pd
import geopandas as gpd
import rasterio
from rasterio.mask import mask as rio_mask
from tqdm import tqdm
import torch
import torch.nn.functional as F
from torchvision import transforms
from PIL import Image
import timm
import simplekml
import matplotlib
matplotlib.use('Agg')          # no display and no GUI loop; jobs run off-thread
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import seaborn as sns
from sklearn.preprocessing import StandardScaler
from sklearn.decomposition import PCA
from sklearn.cluster import KMeans
from sklearn.metrics import (
    confusion_matrix, classification_report,
    silhouette_score, davies_bouldin_score,
    accuracy_score, cohen_kappa_score,
    f1_score
)
from sklearn.manifold import TSNE

warnings.filterwarnings('ignore')

class Config:
    """Pipeline settings. Edit the paths to match your data."""

    # Step 1: where the inputs and outputs live.
    ORTHO_FOLDER = 'data/ortho'           # folder of .tif orthomosaics
    POLY_FOLDER = 'data/geojson'          # folder of crown polygon GeoJSONs
    STEP1_OUTPUT = 'output/step1_output'

    # Step 1: clustering settings.
    K_LIST = [2, 4, 6, 8, 10, 12]         # cluster counts to try
    MODEL_NAME = 'vit_base_patch14_dinov2.lvd142m'
    IMG_SIZE = 224                        # input size DINOv2 expects
    BATCH_SIZE = 32                       # lower this if the GPU runs out of memory
    PCA_COMPONENTS = 50                   # None skips the PCA step
    COPY_TO_CLUSTER_FOLDERS = True        # also copy each crop into its cluster folder

    # Step 2: species assignment.
    CHOSEN_K = 6                          # the k picked after looking at Step 1
    STEP2_OUTPUT = 'output/step2_output'

    # Step 3: validation, if there is ground truth.
    GROUND_TRUTH_CSV = 'data/ground_truth'
    STEP3_VALIDATION_OUTPUT = None        # set automatically when left as None

    # Step 4: KMZ export.
    STEP4_OUTPUT = 'output/step4_output'
    SOURCE_EPSG = 32643                   # the survey's UTM zone; 32643 is UTM 43N
    COLOR_PALETTE = [                     # KML colours, in AABBGGRR order
        '990000ff',  # blue
        '9900ff00',  # green
        '99ff0000',  # red
        '9900ffff',  # yellow
        '99ff00ff',  # magenta
        '99ff8800',  # orange
        '9900ffff',  # cyan
        '99ffffff',  # white
    ]


# Utilities.

device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

def make_dirs(*paths):
    """Create each directory if it is not already there."""
    for p in paths:
        os.makedirs(p, exist_ok=True)

def crown_id_from_gdf(gdf):
    """Read the crown ids out of a GeoDataFrame."""
    for col in ['crown_id', 'id']:
        if col in gdf.columns:
            return gdf[col].astype(int)
    return pd.Series(gdf.index.astype(int))

def normalize_label(val):
    """Normalise a species name: lower case, with spaces and hyphens as
    underscores."""
    return str(val).strip().lower().replace(' ', '_').replace('-', '_')

def auto_detect_csv_columns(df):
    """Work out which CSV columns hold the filenames and the labels."""
    filename_col = label_col = None

    # The filename column is the one whose values look like image filenames.
    for col in df.columns:
        sample = df[col].dropna().astype(str)
        if sample.str.contains(r'\.(tif|tiff|jpg|png)', case=False).mean() > 0.5:
            filename_col = col
            break
    
    if filename_col is None:
        for col in df.columns:
            if df[col].dropna().astype(str).str.contains(r'_tree_', case=False).mean() > 0.3:
                filename_col = col
                break
    
    # The label column is whichever of the rest has the fewest distinct values.
    remaining = [c for c in df.columns if c != filename_col]
    if remaining:
        label_col = min(remaining, key=lambda c: df[c].nunique())
    
    return filename_col, label_col


# Step 1: crop the crowns, extract features, and cluster them.

def step1_crop_crowns(config):
    """Cut each crown polygon out of the orthomosaics as its own image."""
    print('\n' + '='*70)
    print('STEP 1A: CROWN CROPPING')
    print('='*70)
    
    dir_crowns = os.path.join(config.STEP1_OUTPUT, 'crowns')
    make_dirs(dir_crowns)
    
    # Open every orthomosaic, since a crown may come from any of them.
    ortho_srcs = []
    for f in os.listdir(config.ORTHO_FOLDER):
        if f.lower().endswith('.tif'):
            ortho_srcs.append(rasterio.open(os.path.join(config.ORTHO_FOLDER, f)))
    print(f'Orthomosaics found: {len(ortho_srcs)}')
    
    total_saved = total_failed = 0
    
    for gj_file in sorted(os.listdir(config.POLY_FOLDER)):
        if not gj_file.endswith('.geojson'):
            continue
        
        prefix = os.path.splitext(gj_file)[0]
        gdf = gpd.read_file(os.path.join(config.POLY_FOLDER, gj_file))
        gdf['_cid'] = crown_id_from_gdf(gdf)
        gdf = gdf.sort_values('_cid').reset_index(drop=True)
        print(f'\n  {gj_file}  ({len(gdf)} crowns)')
        
        for _, row in tqdm(gdf.iterrows(), total=len(gdf), desc=prefix):
            cid = int(row['_cid'])
            out_name = f'{prefix}_{cid:03d}.tif'
            out_path = os.path.join(dir_crowns, out_name)
            
            if os.path.exists(out_path):
                total_saved += 1
                continue
            
            geom = [row.geometry]
            saved = False
            
            for src in ortho_srcs:
                try:
                    out_img, out_tf = rio_mask(src, geom, crop=True)
                    meta = src.meta.copy()
                    meta.update(height=out_img.shape[1],
                              width=out_img.shape[2],
                              transform=out_tf)
                    with rasterio.open(out_path, 'w', **meta) as dst:
                        dst.write(out_img)
                    saved = True
                    break
                except Exception:
                    continue
            
            if saved:
                total_saved += 1
            else:
                total_failed += 1
    
    for src in ortho_srcs:
        src.close()
    
    print(f'\n✅ Cropping complete — saved: {total_saved}  failed: {total_failed}')
    print(f'   Crown TIFFs in: {dir_crowns}')
    return dir_crowns


def build_dinov2(model_name, img_size):
    """Load the DINOv2 model used to describe each crown.

    It is separate from ``step1_extract_features`` so the worker can build it
    once per process and reuse it; see _get_dinov2 in app/workers/tasks.py.
    """
    model = timm.create_model(model_name, pretrained=True,
                              num_classes=0, img_size=img_size)
    model.eval().to(device)
    return model


def step1_extract_features(config, dir_crowns, model=None):
    """Describe every crown image as a DINOv2 feature vector."""
    print('\n' + '='*70)
    print('STEP 1B: DINOV2 FEATURE EXTRACTION')
    print('='*70)

    dir_features = os.path.join(config.STEP1_OUTPUT, 'features')
    make_dirs(dir_features)

    feat_npy = os.path.join(dir_features, 'dinov2_features.npy')
    feat_csv = os.path.join(dir_features, 'dinov2_features.csv')

    if os.path.exists(feat_npy):
        print('  Cached features found — loading.')
        features = np.load(feat_npy)
        names = pd.read_csv(feat_csv)['image_name'].tolist()
    else:
        tf = transforms.Compose([
            transforms.Resize((config.IMG_SIZE, config.IMG_SIZE)),
            transforms.ToTensor(),
            transforms.Normalize(mean=(0.485, 0.456, 0.406),
                               std=(0.229, 0.224, 0.225)),
        ])

        if model is None:
            model = build_dinov2(config.MODEL_NAME, config.IMG_SIZE)

        img_paths = sorted([os.path.join(dir_crowns, f)
                          for f in os.listdir(dir_crowns)
                          if f.lower().endswith('.tif')])
        print(f'  Images to process: {len(img_paths)}')
        
        features, names = [], []
        for i in tqdm(range(0, len(img_paths), config.BATCH_SIZE), desc='Extracting'):
            batch_paths = img_paths[i:i+config.BATCH_SIZE]
            imgs, nms = [], []
            
            for p in batch_paths:
                try:
                    img = Image.open(p).convert('RGB')
                    imgs.append(tf(img))
                    nms.append(os.path.basename(p))
                except Exception as e:
                    print(f'  ⚠️ Skipped {p}: {e}')
            
            if not imgs:
                continue
            
            batch = torch.stack(imgs).to(device)
            with torch.no_grad():
                feat = model(batch)
            feat = F.normalize(feat, p=2, dim=1)
            features.append(feat.cpu().numpy())
            names.extend(nms)
        
        features = np.vstack(features)
        np.save(feat_npy, features)
        pd.DataFrame({'image_name': names}).to_csv(feat_csv, index=False)
    
    print(f'  Feature matrix: {features.shape}')
    print(f'  Images indexed: {len(names)}')
    
    # Standardise the features, then reduce them with PCA if asked to.
    X = StandardScaler().fit_transform(features)
    # PCA cannot produce more components than min(n_samples, n_features), so cap
    # the configured number. Otherwise a run with few crowns would fail here.
    n_samples, n_features = X.shape
    max_components = min(n_samples, n_features)
    n_components = min(config.PCA_COMPONENTS or 0, max_components)
    if config.PCA_COMPONENTS and n_components >= 1 and n_components < n_features:
        if n_components < config.PCA_COMPONENTS:
            print(f'  PCA components clamped {config.PCA_COMPONENTS} → {n_components} '
                  f'(only {n_samples} samples)')
        X = PCA(n_components=n_components, random_state=42).fit_transform(X)
        print(f'  PCA applied → shape: {X.shape}')
    else:
        print(f'  PCA skipped — using raw standardized features')
    
    names_df = pd.DataFrame({'image_name': names})
    np.save(os.path.join(dir_features, 'X_reduced.npy'), X)
    
    return X, names_df, dir_features


def step1_cluster(config, X, names_df, dir_crowns):
    """Run K-means at each k in the configured list."""
    print('\n' + '='*70)
    print('STEP 1C: MULTI-K CLUSTERING')
    print('='*70)
    
    dir_cluster = os.path.join(config.STEP1_OUTPUT, 'clustering')
    make_dirs(dir_cluster)
    
    inertia_vals = []
    silhouette_vals = []
    db_vals = []
    all_cluster_labels = {}
    
    for k in config.K_LIST:
        print(f'  k={k} ...', end=' ')
        km = KMeans(n_clusters=k, random_state=42, n_init=10)
        cl = km.fit_predict(X)
        all_cluster_labels[k] = cl
        
        inertia_vals.append(km.inertia_)
        sil = silhouette_score(X, cl, sample_size=min(5000, len(X)), random_state=42)
        db = davies_bouldin_score(X, cl)
        silhouette_vals.append(sil)
        db_vals.append(db)
        print(f'inertia={km.inertia_:.0f}  silhouette={sil:.4f}  davies_bouldin={db:.4f}')
        
        # Record which cluster each crown fell into.
        cl_df = names_df.copy()
        cl_df['cluster'] = cl
        cl_df['cluster_label'] = cl_df['cluster'].apply(lambda x: f'cluster_{x}')
        cl_df.to_csv(os.path.join(dir_cluster, f'k{k}_assignments.csv'), index=False)
        
        # One folder per cluster.
        k_dir = os.path.join(dir_cluster, f'k{k}')
        for ci in range(k):
            os.makedirs(os.path.join(k_dir, f'cluster_{ci}'), exist_ok=True)
        
        # Copy each crown image into its cluster's folder, so a person can look
        # through the clusters as ordinary folders of pictures.
        if config.COPY_TO_CLUSTER_FOLDERS:
            for _, row in cl_df.iterrows():
                src = os.path.join(dir_crowns, row['image_name'])
                dst = os.path.join(k_dir, f'cluster_{row["cluster"]}', row['image_name'])
                if os.path.exists(src) and not os.path.exists(dst):
                    shutil.copy2(src, dst)
        
        # An empty species map for the user to fill in.
        blank_map = pd.DataFrame({
            'cluster': list(range(k)),
            'cluster_folder': [f'cluster_{i}' for i in range(k)],
            'species': [''] * k,
            'notes': [''] * k,
        })
        blank_map_path = os.path.join(dir_cluster, f'k{k}_cluster_species_map.csv')
        blank_map.to_csv(blank_map_path, index=False)
    
    print(f'\n✅ Clustering done for all k values.')
    print(f'   ➡ Browse cluster folders in: {dir_cluster}')
    print(f'   ➡ Fill in the species column in k{{chosen_k}}_cluster_species_map.csv')
    
    return all_cluster_labels, inertia_vals, silhouette_vals, db_vals, dir_cluster


def step1_analyze_k(config, inertia_vals, silhouette_vals, db_vals, dir_cluster):
    """Score each k and draw the plot that helps a user choose one."""
    print('\n' + '='*70)
    print('STEP 1D: K-SELECTION ANALYSIS')
    print('='*70)
    
    # Put the three metrics on the same 0-1 scale so they can be combined.
    sil_arr = np.array(silhouette_vals)
    db_arr = np.array(db_vals)
    ine_arr = np.array(inertia_vals)
    
    sil_norm = (sil_arr - sil_arr.min()) / (np.ptp(sil_arr) + 1e-9)
    db_norm = 1 - (db_arr - db_arr.min()) / (np.ptp(db_arr) + 1e-9)
    
    ine_diff = np.abs(np.diff(ine_arr, prepend=ine_arr[0]))
    elbow_norm = (ine_diff - ine_diff.min()) / (np.ptp(ine_diff) + 1e-9)
    
    combined_score = (sil_norm + db_norm + elbow_norm) / 3
    
    rec_df = pd.DataFrame({
        'k': config.K_LIST,
        'inertia': [round(v, 1) for v in inertia_vals],
        'silhouette': [round(v, 4) for v in silhouette_vals],
        'davies_bouldin': [round(v, 4) for v in db_vals],
        'elbow_drop_norm': [round(v, 4) for v in elbow_norm],
        'combined_score': [round(v, 4) for v in combined_score],
    })
    rec_df['rank'] = rec_df['combined_score'].rank(ascending=False).astype(int)
    rec_df = rec_df.sort_values('rank').reset_index(drop=True)
    
    rec_csv = os.path.join(dir_cluster, 'k_recommendation_table.csv')
    rec_df.to_csv(rec_csv, index=False)
    
    print('\n  k-Selection Ranked Table (rank 1 = recommended):')
    print(rec_df.to_string(index=False))
    
    best_k_auto = int(rec_df.iloc[0]['k'])
    print(f'\n  ⭐ Auto-recommended k = {best_k_auto}')
    
    # Draw the three metrics side by side.
    fig, axes = plt.subplots(1, 3, figsize=(15, 4.5))
    
    axes[0].plot(config.K_LIST, inertia_vals, 'o-', linewidth=2, markersize=7)
    axes[0].set_xlabel('k')
    axes[0].set_ylabel('Inertia (WCSS)')
    axes[0].set_title('Elbow Curve', fontweight='bold')
    axes[0].grid(alpha=0.3)
    
    axes[1].plot(config.K_LIST, silhouette_vals, 'o-', linewidth=2, markersize=7)
    axes[1].axvline(best_k_auto, color='red', linestyle='--', linewidth=1.2)
    axes[1].set_xlabel('k')
    axes[1].set_ylabel('Silhouette Score')
    axes[1].set_title('Silhouette Score (higher = better)', fontweight='bold')
    axes[1].grid(alpha=0.3)
    
    axes[2].plot(config.K_LIST, db_vals, 'o-', linewidth=2, markersize=7)
    axes[2].axvline(best_k_auto, color='red', linestyle='--', linewidth=1.2)
    axes[2].set_xlabel('k')
    axes[2].set_ylabel('Davies-Bouldin Index')
    axes[2].set_title('Davies-Bouldin Index (lower = better)', fontweight='bold')
    axes[2].grid(alpha=0.3)
    
    # Work through the figure object rather than pyplot's current figure. Jobs
    # share this process, so plt.savefig and plt.close would act on whichever
    # figure another thread made last.
    fig.suptitle('k-Selection Signals', fontsize=13, fontweight='bold', y=1.02)
    fig.tight_layout()
    fig.savefig(os.path.join(dir_cluster, 'k_selection.png'), dpi=150, bbox_inches='tight')
    plt.close(fig)
    
    print(f'  Saved: clustering/k_selection.png')


def step1_tsne(config, X, names_df, all_cluster_labels, dir_cluster):
    """Draw a t-SNE scatter plot of the crowns for each value of k."""
    print('\n' + '='*70)
    print('STEP 1E: T-SNE VISUALIZATION')
    print('='*70)
    
    tsne_coords_path = os.path.join(dir_cluster, 'tsne_coordinates.csv')
    
    if os.path.exists(tsne_coords_path):
        print('  Cached t-SNE coordinates found — loading.')
        tsne_df = pd.read_csv(tsne_coords_path)
    else:
        print('  Running t-SNE (may take a few minutes)...')
        tsne = TSNE(n_components=2, perplexity=min(30, len(X)-1),
                   random_state=42, init='pca', learning_rate='auto')
        X_tsne = tsne.fit_transform(X)
        tsne_df = pd.DataFrame({
            'x': X_tsne[:, 0],
            'y': X_tsne[:, 1],
            'image_name': names_df['image_name']
        })
        tsne_df.to_csv(tsne_coords_path, index=False)
    
    for k in config.K_LIST:
        tsne_df['cluster'] = all_cluster_labels[k]
        
        fig, ax = plt.subplots(figsize=(8, 6))
        scatter = ax.scatter(tsne_df['x'], tsne_df['y'],
                           c=tsne_df['cluster'], cmap='tab10',
                           s=20, alpha=0.7, linewidths=0)
        
        handles = [mpatches.Patch(color=matplotlib.colormaps['tab10'](i/10),
                                 label=f'Cluster {i}') for i in range(k)]
        ax.legend(handles=handles, bbox_to_anchor=(1.05, 1), loc='upper left',
                 fontsize=9, title=f'k={k}')
        ax.set_title(f't-SNE — k={k}  (n={len(tsne_df)} crowns)',
                    fontsize=13, fontweight='bold')
        ax.set_xlabel('t-SNE 1')
        ax.set_ylabel('t-SNE 2')
        
        fig.tight_layout()
        fig.savefig(os.path.join(dir_cluster, f'tsne_k{k}.png'),
                   dpi=150, bbox_inches='tight')
        plt.close(fig)
    
    print('✅ Step 1 complete.')


# Step 2: turn the cluster names into species labels on every crown.

def step2_assign_species(config):
    """Give every crown the species its cluster was named."""
    print('\n' + '='*70)
    print('STEP 2: SPECIES ASSIGNMENT')
    print('='*70)
    
    # Fill in the folder settings, so this step can be run on its own as long
    # as Step 1's output is already there.
    if not getattr(config, 'ORTHO_FOLDER', None):
        config.ORTHO_FOLDER = os.path.join(config.WORKDIR, 'ortho')
    if not getattr(config, 'POLY_FOLDER', None):
        config.POLY_FOLDER = os.path.join(config.WORKDIR, 'polygons')

    make_dirs(config.STEP2_OUTPUT)
    dir_species = os.path.join(config.STEP2_OUTPUT, 'species_folders')
    make_dirs(dir_species)
    
    # Read the cluster-to-species map the user filled in.
    cluster_map_path = os.path.join(
        config.STEP1_OUTPUT, 'clustering',
        f'k{config.CHOSEN_K}_cluster_species_map.csv'
    )
    
    if not os.path.exists(cluster_map_path):
        raise FileNotFoundError(f'CSV not found: {cluster_map_path}')
    
    cluster_map_df = pd.read_csv(cluster_map_path)
    print(f'  Loaded: {cluster_map_path}')
    
    # Warn about clusters the user left blank.
    blank_rows = cluster_map_df['species'].isna() | (cluster_map_df['species'].astype(str).str.strip() == '')
    if blank_rows.any():
        print(f'\n⚠️  {blank_rows.sum()} cluster(s) still have blank species')
        print('   These will be labeled "unlabelled"')
    
    # Normalise the species names to the pipeline's form.
    cluster_map_df['species'] = (
        cluster_map_df['species']
        .fillna('unlabelled')
        .astype(str).str.strip().str.lower()
        .str.replace(r'[\s\-]+', '_', regex=True)
    )
    
    cluster_to_species = dict(zip(
        cluster_map_df['cluster'].astype(int),
        cluster_map_df['species']
    ))
    
    # Read which cluster each crown was put in, and look up its species.
    cl_csv = os.path.join(config.STEP1_OUTPUT, 'clustering',
                         f'k{config.CHOSEN_K}_assignments.csv')
    assign_df = pd.read_csv(cl_csv)
    assign_df['cluster'] = assign_df['cluster'].astype(int)
    assign_df['species'] = assign_df['cluster'].map(cluster_to_species).fillna('unlabelled')
    
    print(f'\n  Species distribution:')
    sp_counts = assign_df['species'].value_counts()
    for sp, n in sp_counts.items():
        print(f'    {sp:<25}: {n:5d}  ({100*n/len(assign_df):.1f}%)')
    
    # Save the crown-to-species table.
    full_assign_path = os.path.join(config.STEP2_OUTPUT, 'pseudo_label_assignments.csv')
    assign_df.to_csv(full_assign_path, index=False)
    
    # Build the master table: one row per crown polygon.
    poly_rows = []
    for gj_file in sorted(os.listdir(config.POLY_FOLDER)):
        if not gj_file.endswith('.geojson'):
            continue
        
        prefix = os.path.splitext(gj_file)[0]
        gdf_p = gpd.read_file(os.path.join(config.POLY_FOLDER, gj_file))
        
        for idx, row_p in gdf_p.iterrows():
            if 'crown_id' in gdf_p.columns:
                raw_id = row_p['crown_id']
            elif 'id' in gdf_p.columns:
                raw_id = row_p['id']
            else:
                raw_id = idx
            
            try:
                polygon_id = int(raw_id)
            except (ValueError, TypeError):
                polygon_id = str(raw_id).strip()
            
            image_name_full = f'{prefix}_{int(raw_id):03d}.tif'
            image_stem = os.path.splitext(image_name_full)[0]
            
            poly_rows.append({
                'image_name': image_name_full,
                'image_stem': image_stem,
                'polygon_id': polygon_id,
                'site': prefix,
            })
    
    poly_df = pd.DataFrame(poly_rows)
    poly_df = poly_df.merge(
        assign_df[['image_name', 'cluster', 'species']],
        on='image_name', how='left'
    )
    poly_df['species'] = poly_df['species'].fillna('unlabelled')
    
    # Save the master table.
    master_path = os.path.join(config.STEP2_OUTPUT, 'crown_master.csv')
    poly_df.to_csv(master_path, index=False)
    
    poly_csv_path = os.path.join(config.STEP2_OUTPUT, 'polygon_species.csv')
    poly_df[['polygon_id', 'species']].to_csv(poly_csv_path, index=False)
    
    print(f'\n  Files saved:')
    print(f'    crown_master.csv')
    print(f'    polygon_species.csv')
    
    # Copy each crown image into a folder named after its species.
    dir_crowns = os.path.join(config.STEP1_OUTPUT, 'crowns')
    unique_species = sorted(set(v for v in cluster_to_species.values() if v != 'unlabelled'))
    
    for sp in unique_species + ['unlabelled']:
        os.makedirs(os.path.join(dir_species, sp), exist_ok=True)
    
    for _, row in tqdm(assign_df.iterrows(), total=len(assign_df), desc='Copying TIFFs'):
        sp = row['species']
        src = os.path.join(dir_crowns, row['image_name'])
        dst = os.path.join(dir_species, sp, row['image_name'])
        if os.path.exists(src) and not os.path.exists(dst):
            shutil.copy2(src, dst)
    
    print('\n✅ Step 2 complete.')


# Step 3: score the result against ground truth, if there is any.

def step3_validate(config):
    """Compare the assigned species against folder-based ground truth."""
    print('\n' + '='*70)
    print('STEP 3: VALIDATION (FOLDER-BASED)')
    print('='*70)

    GT_FOLDER = config.GROUND_TRUTH_CSV  # reuse variable as folder path

    if not os.path.exists(GT_FOLDER):
        print('❌ Ground truth folder not found')
        return

    val_output = config.STEP3_VALIDATION_OUTPUT or os.path.join(config.STEP2_OUTPUT, 'step3_validation')
    make_dirs(val_output)

    # Read the ground truth: one folder per species, holding crown images.
    gt_rows = []
    for species in os.listdir(GT_FOLDER):
        sp_path = os.path.join(GT_FOLDER, species)
        if not os.path.isdir(sp_path):
            continue

        for fname in os.listdir(sp_path):
            if fname.lower().endswith('.tif'):
                gt_rows.append({
                    'image_name': fname,
                    'true_species': species.lower()
                })

    gt_df = pd.DataFrame(gt_rows)
    print(f'  Ground truth samples: {len(gt_df)}')

    # Read what the pipeline assigned.
    master_path = os.path.join(config.STEP2_OUTPUT, 'crown_master.csv')
    master_df = pd.read_csv(master_path)
    master_df['pred_species'] = master_df['species'].astype(str).str.lower()

    # Keep only the crowns that appear in both.
    val_df = pd.merge(
        gt_df,
        master_df[['image_name', 'pred_species']],
        on='image_name',
        how='inner'
    )

    print(f'  Matched samples: {len(val_df)}')

    if len(val_df) == 0:
        print('❌ No matches found')
        return

    # Score the agreement.
    y_true = val_df['true_species']
    y_pred = val_df['pred_species']

    acc = accuracy_score(y_true, y_pred)
    kappa = cohen_kappa_score(y_true, y_pred)
    f1_mac = f1_score(y_true, y_pred, average='macro', zero_division=0)

    print(f'\n  Accuracy: {acc*100:.1f}%')
    print(f'  Cohen Kappa: {kappa:.4f}')
    print(f'  F1 (macro): {f1_mac:.4f}')

    # Draw the confusion matrix.
    classes = sorted(set(y_true.unique()) | set(y_pred.unique()))
    cm = confusion_matrix(y_true, y_pred, labels=classes)

    fig, ax = plt.subplots(figsize=(8, 6))
    sns.heatmap(cm, annot=True, fmt='d',
               xticklabels=classes, yticklabels=classes,
               cmap='YlOrRd', ax=ax)
    ax.set_xlabel('Predicted')
    ax.set_ylabel('True')
    ax.set_title(f'Confusion Matrix (Accuracy: {acc*100:.1f}%)')

    fig.tight_layout()
    fig.savefig(os.path.join(val_output, 'confusion_matrix.png'), dpi=150)
    plt.close(fig)

    val_df.to_csv(os.path.join(val_output, 'validation_detail.csv'), index=False)

    print('\n✅ Folder-based validation complete.')


# Step 4: export the species map as a KMZ for Google Earth.

def step4_export_kmz(config):
    """Write the species map as a KMZ that Google Earth can open."""
    print('\n' + '='*70)
    print('STEP 4: KMZ EXPORT')
    print('='*70)

    if not getattr(config, 'POLY_FOLDER', None):
        config.POLY_FOLDER = os.path.join(config.WORKDIR, 'polygons')

    make_dirs(config.STEP4_OUTPUT)

    # Read the master table written by Step 2.
    master_path = os.path.join(config.STEP2_OUTPUT, 'crown_master.csv')
    master_df = pd.read_csv(master_path)
    
    # Read the crown polygons.
    all_polys = []
    for gj_file in sorted(os.listdir(config.POLY_FOLDER)):
        if not gj_file.endswith('.geojson'):
            continue
        
        prefix = os.path.splitext(gj_file)[0]
        g = gpd.read_file(os.path.join(config.POLY_FOLDER, gj_file))
        g['_cid'] = crown_id_from_gdf(g)
        g['image_name'] = g['_cid'].apply(lambda x: f'{prefix}_{int(x):03d}.tif')
        
        if 'Confidence_score' in g.columns:
            g = g.rename(columns={'Confidence_score': 'confidence_score'})
        
        all_polys.append(g)
    
    gdf_all = pd.concat(all_polys, ignore_index=True)
    
    # Attach each polygon's species.
    gdf_all = gdf_all.merge(
        master_df[['image_name', 'species', 'polygon_id']],
        on='image_name', how='left'
    )
    gdf_all['species'] = gdf_all['species'].fillna('unlabelled')
    
    # KML needs latitude and longitude, so reproject to WGS84.
    if gdf_all.crs is None:
        gdf_all = gdf_all.set_crs(epsg=config.SOURCE_EPSG)
    gdf_wgs = gdf_all.to_crs(epsg=4326)
    
    print(f'  Total polygons: {len(gdf_wgs)}')
    print(f'  Labeled: {gdf_wgs["species"].ne("unlabelled").sum()}')
    
    # Build the KML, one colour per species.
    species_list = sorted(gdf_wgs['species'].unique())
    if 'unlabelled' in species_list:
        species_list.remove('unlabelled')
        species_list.append('unlabelled')
    
    species_color_idx = {sp: i % len(config.COLOR_PALETTE) for i, sp in enumerate(species_list)}
    
    kml = simplekml.Kml()
    kml.document.name = 'Tree Crown Species Map'
    
    kml_folders = {}
    kml_styles = {}
    
    for sp in species_list:
        ci = species_color_idx[sp]
        color = config.COLOR_PALETTE[ci]
        
        style = simplekml.Style()
        style.polystyle.color = color
        style.polystyle.fill = 1
        style.polystyle.outline = 1
        style.linestyle.color = 'ff000000'
        style.linestyle.width = 1
        
        kml_styles[sp] = style
        folder_name = sp.replace('_', ' ').title()
        kml_folders[sp] = kml.newfolder(name=folder_name)
    
    added = 0
    for _, row in tqdm(gdf_wgs.iterrows(), total=len(gdf_wgs), desc='Building KMZ'):
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue
        if geom.geom_type == 'MultiPolygon':
            geom = max(geom.geoms, key=lambda g: g.area)
        if geom.geom_type != 'Polygon':
            continue
        
        sp = row.get('species', 'unlabelled')
        polygon_id = row.get('polygon_id', 'NA')
        
        folder = kml_folders.get(sp, kml_folders.get('unlabelled'))
        style = kml_styles.get(sp, kml_styles.get('unlabelled'))
        
        pol = folder.newpolygon(
            name=f'{sp} | {polygon_id}',
            outerboundaryis=list(geom.exterior.coords)
        )
        pol.style = style
        pol.description = f'<b>Polygon ID:</b> {polygon_id}<br><b>Species:</b> {sp}'
        added += 1
    
    print(f'  Polygons added: {added}')
    
    # Save the KMZ.
    kml_path = os.path.join(config.STEP4_OUTPUT, 'doc.kml')
    kmz_path = os.path.join(config.STEP4_OUTPUT, 'species_map.kmz')
    
    kml.save(kml_path)
    with zipfile.ZipFile(kmz_path, 'w', zipfile.ZIP_DEFLATED) as kmz:
        kmz.write(kml_path, 'doc.kml')
    os.remove(kml_path)
    
    size_mb = os.path.getsize(kmz_path) / 1e6
    print(f'\n✅ KMZ saved: {kmz_path}')
    print(f'   File size: {size_mb:.1f} MB')
    print(f'   → Open in Google Earth Pro or earth.google.com')


# Running the whole pipeline.

def load_config(config_path):
    """Load the Config class from a config file given on the command line.

    The file has to define a class called 'Config'. For example:
    python tree_crown_pipeline.py --step 1 --config config.py
    """
    import importlib.util
    config_path = os.path.abspath(config_path)
    if not os.path.exists(config_path):
        raise FileNotFoundError(f'Config file not found: {config_path}')
    spec = importlib.util.spec_from_file_location('user_config', config_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, 'Config'):
        raise AttributeError(
            f"Config file '{config_path}' must define a class named 'Config'.\n"
            f"See config_example.py for the expected format."
        )
    return module.Config()


def main():
    """Command-line entry point: run one step, or all of them."""
    parser = argparse.ArgumentParser(
        description='Tree Crown Species Classification Pipeline',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python tree_crown_pipeline.py --step 1 --config config.py
  python tree_crown_pipeline.py --step 2 --config config.py
  python tree_crown_pipeline.py --step 3 --config config.py
  python tree_crown_pipeline.py --step 4 --config config.py
  python tree_crown_pipeline.py --step all --config config.py

Workflow:
  1. Copy config_example.py to config.py
  2. Edit config.py with your paths
  3. Run step 1, browse cluster folders, fill species CSV
  4. Update CHOSEN_K in config.py, run step 2
  5. (Optional) Run step 3 for validation
  6. Run step 4 to export KMZ for Google Earth
        """
    )
    parser.add_argument(
        '--step', type=str, choices=['1', '2', '3', '4', 'all'],
        default='all', help='Which step to run (default: all)'
    )
    parser.add_argument(
        '--config', type=str, default=None,
        help='Path to config file (default: uses built-in Config class)'
    )
    args = parser.parse_args()

    # Load the configuration.
    if args.config:
        print(f'Loading config from: {args.config}')
        config = load_config(args.config)
        print('✅ External config loaded successfully.')
    else:
        print('No --config file specified. Using built-in Config class.')
        print('Tip: copy config_example.py → config.py, edit paths, then run:')
        print('     python tree_crown_pipeline.py --step 1 --config config.py')
        config = Config()
    
    print('\n' + '='*70)
    print('TREE CROWN SPECIES CLASSIFICATION PIPELINE')
    print('='*70)
    print(f'Device: {device}')
    print(f'Step to run: {args.step}')
    
    # Run the requested step.
    if args.step in ['1', 'all']:
        dir_crowns = step1_crop_crowns(config)
        X, names_df, dir_features = step1_extract_features(config, dir_crowns)
        all_cluster_labels, inertia_vals, silhouette_vals, db_vals, dir_cluster = step1_cluster(
            config, X, names_df, dir_crowns
        )
        step1_analyze_k(config, inertia_vals, silhouette_vals, db_vals, dir_cluster)
        step1_tsne(config, X, names_df, all_cluster_labels, dir_cluster)
        
        print('\n' + '='*70)
        print('STEP 1 COMPLETE')
        print('='*70)
        print(f'\n📁 Outputs in: {config.STEP1_OUTPUT}')
        print('\n🔴 NEXT STEPS:')
        print('  1. Browse cluster folders in: clustering/k{{k}}/')
        print('  2. Choose your k value')
        print('  3. Fill in the species column in: clustering/k{{k}}_cluster_species_map.csv')
        print('  4. Update Config.CHOSEN_K in this script')
        print('  5. Run: python tree_crown_pipeline.py --step 2')
        
        if args.step == '1':
            return
    
    if args.step in ['2', 'all']:
        step2_assign_species(config)
        
        print('\n' + '='*70)
        print('STEP 2 COMPLETE')
        print('='*70)
        print(f'\n📁 Outputs in: {config.STEP2_OUTPUT}')
        print('\n🔴 NEXT STEPS:')
        print('  - (Optional) Run validation: python tree_crown_pipeline.py --step 3')
        print('  - Export to KMZ: python tree_crown_pipeline.py --step 4')
        
        if args.step == '2':
            return
    
    if args.step in ['3', 'all']:
        step3_validate(config)
        
        if args.step == '3':
            return
    
    if args.step in ['4', 'all']:
        step4_export_kmz(config)
        
        print('\n' + '='*70)
        print('PIPELINE COMPLETE')
        print('='*70)
        print(f'\n📁 Final outputs:')
        print(f'  - Species assignments: {config.STEP2_OUTPUT}/crown_master.csv')
        print(f'  - Google Earth KMZ: {config.STEP4_OUTPUT}/species_map.kmz')
        if os.path.exists(config.GROUND_TRUTH_CSV):
            val_output = config.STEP3_VALIDATION_OUTPUT or os.path.join(config.STEP2_OUTPUT, 'step3_validation')
            print(f'  - Validation results: {val_output}/')


if __name__ == '__main__':
    main()
