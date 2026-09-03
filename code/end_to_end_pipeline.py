import os
import shutil
import argparse

from config import Config

from tree_crown_pipeline import (
    step1_crop_crowns,
    step1_extract_features,
    step1_cluster,
    step1_analyze_k,
    step1_tsne,
    step2_assign_species,
    step3_validate,
    step4_export_kmz
)

from predict import run_detectree2_pipeline


# Step 0: detect the crowns with Detectree2.

def step0_detection(config):

    print("\nSTEP 0: Detectree2")

    out_dir = os.path.join(config.WORKDIR, "detectree")
    os.makedirs(out_dir, exist_ok=True)

    geojson_path, _, used_ortho_path = run_detectree2_pipeline(
        ortho_path=config.ORTHO_PATH,
        model_path=config.DETECTREE_MODEL,
        output_dir=out_dir,
        tile_size=config.TILE_SIZE,
        buffer=config.BUFFER,
        iou_threshold=config.IOU_THRESHOLD,
        conf_threshold=config.CONF_THRESHOLD
    )

    # Set up the folders the later steps read from.
    ortho_dir = os.path.join(config.WORKDIR, "ortho")
    poly_dir = os.path.join(config.WORKDIR, "polygons")

    os.makedirs(ortho_dir, exist_ok=True)
    os.makedirs(poly_dir, exist_ok=True)

    # Copy the original orthomosaic, not the downsampled one, so the crown
    # crops are cut at full resolution.
    shutil.copy(config.ORTHO_PATH, os.path.join(ortho_dir, "image.tif"))
    shutil.copy(geojson_path, os.path.join(poly_dir, "crowns.geojson"))

    print("✅ Step 0 complete")

# Step 1: crop the crowns and cluster them.

def step1_clustering(config):

    print("\nSTEP 1: Cropping + Clustering")

    config.ORTHO_FOLDER = os.path.join(config.WORKDIR, "ortho")
    config.POLY_FOLDER = os.path.join(config.WORKDIR, "polygons")

    crowns_dir = step1_crop_crowns(config)

    X, names_df, _ = step1_extract_features(config, crowns_dir)

    all_cluster_labels, inertia_vals, silhouette_vals, db_vals, dir_cluster = step1_cluster(
        config, X, names_df, crowns_dir
    )

    step1_analyze_k(config, inertia_vals, silhouette_vals, db_vals, dir_cluster)
    step1_tsne(config, X, names_df, all_cluster_labels, dir_cluster)

    print("\n✅ Step 1 complete")

    print("\n🔴 IMPORTANT NEXT STEP:")
    print("👉 Open this folder:")
    print(f"{config.STEP1_OUTPUT}/clustering/")
    print("\n👉 Choose best k from plots")
    print("👉 Open: k{K}_cluster_species_map.csv")
    print("👉 Fill species column")
    print("👉 Then update CHOSEN_K in config.py")



# Step 2: apply the species names.

def step2_species(config):

    print("\nSTEP 2: Species Assignment")

    step2_assign_species(config)

    print("✅ Step 2 complete")


# Step 3: validate against ground truth.

def step3_validation(config):

    if getattr(config, 'GROUND_TRUTH_CSV', None):
        print("\nSTEP 3: Validation")
        step3_validate(config)
    else:
        print("\nSTEP 3: Validation skipped because GROUND_TRUTH_CSV is not set in config")


# Step 4: export the KMZ.

def step4_kmz(config):

    print("\nSTEP 4: KMZ Export")

    step4_export_kmz(config)

    print("✅ Step 4 complete")


# Command-line entry point.

def main():

    parser = argparse.ArgumentParser()
    parser.add_argument("--step", type=int, required=True,
                        help="0: detect | 1: cluster | 2: species | 3: validation | 4: kmz")

    args = parser.parse_args()

    config = Config()

    if args.step == 0:
        step0_detection(config)

    elif args.step == 1:
        step1_clustering(config)

    elif args.step == 2:
        step2_species(config)

    elif args.step == 3:
        step3_validation(config)

    elif args.step == 4:
        step4_kmz(config)

    else:
        print("Invalid step")


if __name__ == "__main__":
    main()
