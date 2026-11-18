"""Per-orthomosaic views of a multi-ortho run.

Clustering runs once over the crowns of every ortho in a run, so cluster ids
mean the same thing on every site. These helpers split what the run already
wrote by site, without re-running anything:

  * which site a crown belongs to (its image name is ``<stem>_<nnn>.tif``)
  * species counts per site, from ``crown_master.csv``
  * cluster members per site, for the review thumbnails
  * the t-SNE plot for one k, showing only one site's crowns
  * the k-selection plot, with each metric computed over one site's crowns
    under the shared clustering

Plots are drawn on first request and cached under
``step1_output/clustering/sites/<stem>/``. A cached plot is redrawn when any
file it was drawn from is newer.
"""
from __future__ import annotations

import csv
import os

from app.core.logging import get_logger

import map_images

log = get_logger("app.site_plots")

SITES_SUBDIR = "sites"
# Crowns per side (this site / the rest) fed to the silhouette.
_SIL_CAP = 2500


# -- which site a crown belongs to -------------------------------------------

def crown_site(name: str, stems) -> str | None:
    """The stem ``name`` (``<stem>_<nnn>.tif``) belongs to, or None.

    The longest matching stem wins, so ``site_a_2`` is not taken for ``site_a``.
    """
    base = os.path.splitext(os.path.basename(name or ""))[0]
    best = None
    for stem in stems:
        if base.startswith(stem + "_") and (best is None or len(stem) > len(best)):
            best = stem
    return best


def species_by_site(master_csv: str, stems) -> dict[str, dict[str, int]]:
    """``{stem: {species: count}}`` from ``crown_master.csv``.

    Uses the ``site`` column when the run wrote one, else the crown's name.
    """
    out: dict[str, dict[str, int]] = {s: {} for s in stems}
    if not os.path.exists(master_csv):
        return out
    try:
        with open(master_csv, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                site = (row.get("site") or "").strip() or crown_site(
                    row.get("image_name") or row.get("image_stem") or "", stems)
                if site not in out:
                    continue
                sp = (row.get("species") or "unlabelled").strip() or "unlabelled"
                out[site][sp] = out[site].get(sp, 0) + 1
    except Exception:                    # noqa: BLE001
        log.warning("could not split %s by site", master_csv, exc_info=True)
    return out


def filter_members(rows: dict, site: str | None, stems) -> dict:
    """Keep only ``site``'s crowns in ``{cluster: [(name, dist), ...]}``."""
    if not site:
        return rows
    return {ci: [m for m in members if crown_site(m[0], stems) == site]
            for ci, members in rows.items()}


# -- plots --------------------------------------------------------------------

def _site_dir(cluster_dir: str, stem: str) -> str:
    return os.path.join(cluster_dir, SITES_SUBDIR, stem)


def _fresh(dst: str, sources) -> bool:
    if not os.path.exists(dst):
        return False
    t = os.path.getmtime(dst)
    return all(os.path.getmtime(s) <= t for s in sources if os.path.exists(s))


def _cluster_colors(k: int):
    # Same colours as tree_crown_pipeline.cluster_colors, without importing torch.
    import matplotlib
    if k <= 10:
        return [matplotlib.colormaps["tab10"](i) for i in range(k)]
    if k <= 20:
        return [matplotlib.colormaps["tab20"](i) for i in range(k)]
    return [matplotlib.colormaps["turbo"](i / (k - 1)) for i in range(k)]


def _pyplot():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.patches as mpatches
    import matplotlib.pyplot as plt
    return plt, mpatches


def _save(fig, dst: str, plt) -> bool:
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = map_images._atomic_path(dst)
    try:
        fig.savefig(tmp, dpi=150, bbox_inches="tight")
        os.replace(tmp, dst)
    finally:
        plt.close(fig)
        if os.path.exists(tmp):
            os.remove(tmp)
    return os.path.exists(dst)


def tsne_site_png(cluster_dir: str, k: int, site: str, stems) -> str | None:
    """t-SNE for one k with only ``site``'s crowns, on the full plot's axes.

    The axes match the all-sites plot, so the two can be compared by eye.
    """
    coords = os.path.join(cluster_dir, "tsne_coordinates.csv")
    assign = os.path.join(cluster_dir, f"k{k}_assignments.csv")
    dst = os.path.join(_site_dir(cluster_dir, site), f"tsne_k{k}.png")
    if not (os.path.exists(coords) and os.path.exists(assign)):
        return None
    if _fresh(dst, (coords, assign)):
        return dst
    try:
        import pandas as pd
        plt, mpatches = _pyplot()

        df = pd.read_csv(coords).merge(
            pd.read_csv(assign)[["image_name", "cluster"]], on="image_name", how="left")
        mine = df[df["image_name"].map(lambda n: crown_site(n, stems) == site)]
        colors = _cluster_colors(k)

        fig, ax = plt.subplots(figsize=(8, 6))
        # The other sites, faint, so this site's place in the whole is visible.
        rest = df.drop(mine.index)
        if len(rest):
            ax.scatter(rest["x"], rest["y"], c="#d0d0d0", s=12, alpha=0.5, linewidths=0)
        ax.scatter(mine["x"], mine["y"],
                   c=[colors[int(c)] if c == c else "#888888" for c in mine["cluster"]],
                   s=20, alpha=0.8, linewidths=0)
        handles = [mpatches.Patch(color=colors[i],
                                  label=f"Cluster {i} ({int((mine['cluster'] == i).sum())})")
                   for i in range(k)]
        if len(rest):
            handles.append(mpatches.Patch(color="#d0d0d0", label="other orthomosaics"))
        ax.legend(handles=handles, bbox_to_anchor=(1.05, 1), loc="upper left",
                  fontsize=9, title=f"k={k}")
        pad_x = (df["x"].max() - df["x"].min()) * 0.04 or 1
        pad_y = (df["y"].max() - df["y"].min()) * 0.04 or 1
        ax.set_xlim(df["x"].min() - pad_x, df["x"].max() + pad_x)
        ax.set_ylim(df["y"].min() - pad_y, df["y"].max() + pad_y)
        ax.set_title(f"t-SNE — k={k} — {site}  (n={len(mine)} of {len(df)} crowns)",
                     fontsize=13, fontweight="bold")
        ax.set_xlabel("t-SNE 1")
        ax.set_ylabel("t-SNE 2")
        fig.tight_layout()
        return dst if _save(fig, dst, plt) else None
    except Exception:                    # noqa: BLE001
        log.warning("per-site t-SNE not drawn dir=%s k=%s site=%s",
                    cluster_dir, k, site, exc_info=True)
        return None


def k_selection_site_png(step1_dir: str, site: str, stems,
                         recommended_k: int | None = None) -> str | None:
    """The k-selection plot, each metric over ``site``'s crowns only.

    Clusters and their centres stay those of the whole run, so a k is judged by
    how well this site's crowns fit the groups everyone will label:

      * inertia: sum of squared distances to the run's cluster centres
        (``dist_to_centroid`` squared, as KMeans measured it)
      * silhouette: mean silhouette of this site's crowns, computed against
        every crown of the run
      * Davies-Bouldin: over this site's crowns and the clusters they fall in
    """
    cdir = os.path.join(step1_dir, "clustering")
    fdir = os.path.join(step1_dir, "features")
    x_path = os.path.join(fdir, "X_reduced.npy")
    names_path = os.path.join(fdir, "dinov2_features.csv")
    dst = os.path.join(_site_dir(cdir, site), "k_selection.png")
    try:
        ks = sorted(int(f[1:-len("_assignments.csv")]) for f in os.listdir(cdir)
                    if f.startswith("k") and f.endswith("_assignments.csv")
                    and f[1:-len("_assignments.csv")].isdigit())
    except OSError:
        return None
    if not ks:
        return None
    sources = [x_path, names_path] + [os.path.join(cdir, f"k{k}_assignments.csv") for k in ks]
    if _fresh(dst, sources):
        return dst
    try:
        import numpy as np
        import pandas as pd
        from sklearn.metrics import davies_bouldin_score, silhouette_samples

        have_x = os.path.exists(x_path) and os.path.exists(names_path)
        X = np.load(x_path) if have_x else None
        names = pd.read_csv(names_path)["image_name"].tolist() if have_x else None
        if X is not None and len(X) != len(names):
            X = None

        inertia, sil, dbi = [], [], []
        n_site = 0
        for k in ks:
            a = pd.read_csv(os.path.join(cdir, f"k{k}_assignments.csv"))
            mask = a["image_name"].map(lambda n: crown_site(n, stems) == site).to_numpy()
            n_site = int(mask.sum())
            if "dist_to_centroid" in a.columns:
                inertia.append(float((a.loc[mask, "dist_to_centroid"] ** 2).sum()))
            else:
                inertia.append(float("nan"))
            s = d = float("nan")
            if X is not None and n_site >= 2:
                order = pd.Series(range(len(names)), index=names)
                idx = order.reindex(a["image_name"]).to_numpy()
                if not np.isnan(idx).any():
                    Xa = X[idx.astype(int)]
                    labels = a["cluster"].to_numpy()
                    if len(set(labels)) >= 2:
                        # Silhouette is O(n^2) in memory; cap the crowns used,
                        # as the pipeline does (sample_size=5000).
                        rng = np.random.default_rng(42)
                        site_i, rest_i = np.flatnonzero(mask), np.flatnonzero(~mask)
                        if len(site_i) > _SIL_CAP:
                            site_i = rng.choice(site_i, _SIL_CAP, replace=False)
                        if len(rest_i) > _SIL_CAP:
                            rest_i = rng.choice(rest_i, _SIL_CAP, replace=False)
                        pick = np.concatenate([site_i, rest_i])
                        if len(set(labels[pick])) >= 2:
                            s = float(silhouette_samples(Xa[pick], labels[pick])
                                      [:len(site_i)].mean())
                    if len(set(labels[mask])) >= 2:
                        d = float(davies_bouldin_score(Xa[mask], labels[mask]))
            sil.append(s)
            dbi.append(d)

        plt, _ = _pyplot()
        fig, axes = plt.subplots(1, 3, figsize=(15, 4.5))
        panels = ((inertia, "Inertia (WCSS)", "Elbow Curve"),
                  (sil, "Silhouette Score", "Silhouette Score (higher = better)"),
                  (dbi, "Davies-Bouldin Index", "Davies-Bouldin Index (lower = better)"))
        for i, (ax, (vals, ylabel, title)) in enumerate(zip(axes, panels)):
            ax.plot(ks, vals, "o-", linewidth=2, markersize=7)
            if i and recommended_k in ks:
                ax.axvline(recommended_k, color="red", linestyle="--", linewidth=1.2)
            ax.set_xlabel("k")
            ax.set_ylabel(ylabel)
            ax.set_title(title, fontweight="bold")
            ax.grid(alpha=0.3)
        fig.suptitle(f"k-Selection Signals — {site}  ({n_site} crowns, clustered with "
                     f"the whole run)", fontsize=13, fontweight="bold", y=1.02)
        fig.tight_layout()
        return dst if _save(fig, dst, plt) else None
    except Exception:                    # noqa: BLE001
        log.warning("per-site k-selection not drawn dir=%s site=%s",
                    step1_dir, site, exc_info=True)
        return None
