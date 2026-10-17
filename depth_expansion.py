import argparse
import json
from pathlib import Path
import numpy as np
import torch
import torch.nn.functional as F
from scipy.cluster.hierarchy import linkage, fcluster
from tqdm import tqdm

def hierarchical_clustering_with_external_primary(video_features, cluster_ids, relevance_scores, num_subclusters=5, num_subsubclusters=5):
    clusters = {i: {} for i in range(0, max(cluster_ids)+1)}

    for cluster_id in set(cluster_ids):
        primary_indices = [i for i, x in enumerate(cluster_ids) if x == cluster_id]

        if cluster_id < len(relevance_scores):
            score = relevance_scores[cluster_id]
        else:
            score = 3

        if len(primary_indices) < 2:
            clusters[cluster_id] = primary_indices
            continue

        sub_features = video_features[primary_indices]

        if score == 1:
            clusters[cluster_id] = primary_indices
            continue

        linked_sub = linkage(sub_features, method='ward')
        sub_cluster_labels = fcluster(linked_sub, num_subclusters, criterion='maxclust')
        sub_cluster_labels = sub_cluster_labels - 1

        if score == 2:
            clusters[cluster_id] = {i: [primary_indices[j] for j in np.where(sub_cluster_labels == i)[0]] for i in range(0, num_subclusters)}
            continue

        for subcluster_id in sorted(set(sub_cluster_labels.tolist())):
            sub_indices = np.where(sub_cluster_labels == subcluster_id)[0]
            if len(sub_indices) < 2:
                clusters[cluster_id][subcluster_id] = [primary_indices[j] for j in sub_indices]
                continue

            subsub_features = sub_features[sub_indices]
            linked_subsub = linkage(subsub_features, method='ward')
            subsub_cluster_labels = fcluster(linked_subsub, num_subsubclusters, criterion='maxclust')
            subsub_cluster_labels = subsub_cluster_labels - 1

            clusters[cluster_id][subcluster_id] = {}
            for subsubcluster_id in sorted(set(subsub_cluster_labels.tolist())):
                final_indices = sub_indices[np.where(subsub_cluster_labels == subsubcluster_id)[0]]  # Correctly index into sub_indices
                original_indices = [primary_indices[i] for i in final_indices]
                clusters[cluster_id][subcluster_id][subsubcluster_id] = original_indices

    return clusters


def cosine_similarity(points, centroid):
    """
    Calculate cosine similarity between points and centroid.
    Returns the cosine distances (1 - similarity).
    """
    points_normalized = F.normalize(points, dim=1)
    centroid_normalized = F.normalize(centroid.unsqueeze(0), dim=1)
    return 1 - torch.mm(points_normalized, centroid_normalized.T).squeeze()

def find_closest_points_in_temporal_order_subsub(x, clusters, relevance_scores):
    closest_points_indices = []

    for cluster_id, cluster_data in clusters.items():
        # print("cluster_id",cluster_id)
        # print("cluster_data type",type(cluster_data))

        if cluster_id < len(relevance_scores):
            relevance = relevance_scores[cluster_id]
        else:
            relevance = 3

        if isinstance(cluster_data, list):  # Primary cluster directly
            cluster_data = np.array(cluster_data)
            if cluster_data.size == 0:
                continue  # Skip empty clusters
            points_in_cluster = x[torch.tensor(cluster_data, dtype=torch.long)]
            cluster_centroid = points_in_cluster.mean(dim=0)
            distances = cosine_similarity(points_in_cluster, cluster_centroid)
            if distances.numel() > 0:
                closest_idx = torch.argmin(distances).item()
                closest_points_indices.append(int(cluster_data[closest_idx]))

        elif isinstance(cluster_data, dict):  # Handle subclusters and sub-subclusters
            if relevance == 1:
                # Only take the representative frame for the primary cluster
                print("line 207")
                primary_indices = []
                for subcluster_data in cluster_data.values():
                    if isinstance(subcluster_data, dict):
                        for sub_data in subcluster_data.values():
                            if sub_data.size > 0:
                                primary_indices.append(sub_data)
                    elif isinstance(subcluster_data, list) and len(subcluster_data) > 0:
                        subcluster_data = np.array(subcluster_data)
                        primary_indices.append(subcluster_data)

                if primary_indices:
                    primary_indices = np.concatenate(primary_indices)
                    primary_points = x[torch.tensor(primary_indices, dtype=torch.long)]
                    primary_centroid = primary_points.mean(dim=0)
                    primary_distances = cosine_similarity(primary_points, primary_centroid)
                    if primary_distances.numel() > 0:
                        closest_primary_idx = torch.argmin(primary_distances).item()
                        closest_points_indices.append(int(primary_indices[closest_primary_idx]))
                continue

            elif relevance == 2 or relevance == 3:
                # Include primary cluster representative
                primary_indices = []
                for subcluster_data in cluster_data.values():
                    if isinstance(subcluster_data, dict):
                        for sub_data in subcluster_data.values():
                            if len(sub_data) > 0:
                                primary_indices.append(sub_data)
                    elif isinstance(subcluster_data, list) and len(subcluster_data) > 0:
                        subcluster_data = np.array(subcluster_data)
                        primary_indices.append(subcluster_data)

                if primary_indices:
                    primary_indices = np.concatenate(primary_indices)
                    primary_points = x[torch.tensor(primary_indices, dtype=torch.long)]
                    primary_centroid = primary_points.mean(dim=0)
                    primary_distances = cosine_similarity(primary_points, primary_centroid)
                    if primary_distances.numel() > 0:
                        closest_primary_idx = torch.argmin(primary_distances).item()
                        closest_points_indices.append(int(primary_indices[closest_primary_idx]))

                for subcluster_id, subclusters in cluster_data.items():
                    if isinstance(subclusters, dict):  # Sub-subclusters
                        for subsubcluster_id, indices in subclusters.items():
                            if len(indices) == 0:
                                continue  # Skip empty sub-subclusters
                            indices_tensor = torch.tensor(indices, dtype=torch.long)
                            points_in_subsubcluster = x[indices_tensor]
                            subsubcluster_centroid = points_in_subsubcluster.mean(dim=0)
                            distances = cosine_similarity(points_in_subsubcluster, subsubcluster_centroid)
                            if distances.numel() > 0:
                                closest_idx_in_subsubcluster = torch.argmin(distances).item()
                                closest_global_idx = indices[closest_idx_in_subsubcluster]
                                closest_points_indices.append(int(closest_global_idx))

                    elif isinstance(subclusters, list):
                        subclusters = np.array(subclusters)
                        if subclusters.size == 0:
                            continue  # Skip empty subclusters
                        points_in_subcluster = x[torch.tensor(subclusters, dtype=torch.long)]
                        subcluster_centroid = points_in_subcluster.mean(dim=0)
                        distances = cosine_similarity(points_in_subcluster, subcluster_centroid)
                        if distances.numel() > 0:
                            closest_idx = torch.argmin(distances).item()
                            closest_points_indices.append(int(subclusters[closest_idx]))

    return sorted(set(closest_points_indices))



def load_image_features(name_ids, save_folder):
    """
    Load image features from a .pt file.

    Args:
    - filename (str): Name of the .pt file to load

    Returns:
    - img_feats (torch.Tensor): Loaded image features
    """
    filename = f"{name_ids}.pt"  # Construct filename with name_ids
    filepath = Path(save_folder) / filename
    img_feats = torch.load(filepath, map_location="cpu", weights_only=True)
    return img_feats

def load_json(fn):
    with open(fn, 'r') as f:
        data = json.load(f)
    return data

def save_json(data, fn, indent=4):
    with open(fn, 'w') as f:
        json.dump(data, f, indent=indent)


def depth_expansion():
    parser = argparse.ArgumentParser(description="Expand the relevant VideoTree clusters.")
    parser.add_argument("--frame_feat_path", default="data/EgoSchema/egoschema_features")
    parser.add_argument("--relevance_path", default="workdirs/videotree-smoke/relevance_score.json")
    parser.add_argument("--width_path", default="workdirs/videotree-smoke/width_res.json")
    parser.add_argument("--output_path", default="workdirs/videotree-smoke/depth_expansion_res.json")
    parser.add_argument("--num_subclusters", type=int, default=4)
    parser.add_argument("--num_subsubclusters", type=int, default=4)
    args = parser.parse_args()
    cap_scores = load_json(args.relevance_path)
    cap_scores = cap_scores.get("data", cap_scores)
    width = load_json(args.width_path)
    rows = []
    for item in tqdm(width):
        uid = item["name"]
        features = load_image_features(uid, args.frame_feat_path)
        result = cap_scores[uid]
        scores = result.get("cluster_relevance")
        if scores is None:
            raise ValueError("Re-run width expansion to obtain scores aligned to cluster IDs.")
        labels = item["cluster_ids_x"]
        if len(labels) != len(features) or len(scores) <= max(labels):
            raise ValueError(f"Invalid cluster assignments or scores for {uid}.")
        clusters = hierarchical_clustering_with_external_primary(
            features, labels, scores, args.num_subclusters, args.num_subsubclusters)
        selected = find_closest_points_in_temporal_order_subsub(features, clusters, scores)
        if not selected:
            raise ValueError(f"Depth expansion selected no frames for {uid}.")
        rows.append({"name": uid, "sorted_values": selected, "relevance": scores})
    Path(args.output_path).parent.mkdir(parents=True, exist_ok=True)
    save_json(rows, args.output_path)


if __name__ == "__main__":
    depth_expansion()
