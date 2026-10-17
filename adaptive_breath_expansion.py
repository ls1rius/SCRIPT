import os
from pathlib import Path
from util import *
from eval import *
from dataset import get_dataset
from prompts import PromptFactory
from model import get_model
from tqdm import tqdm
from pprint import pprint
from kmeans_pytorch import kmeans
import torch

def load_frame_features(name_ids, save_folder):
    """
    Load frame features from a .pt file.

    Args:
    - filename (str): Name of the .pt file to load

    Returns:
    - img_feats (torch.Tensor): Loaded image features
    """
    filename = f"{name_ids}.pt"  # Construct filename with name_ids
    filepath = os.path.join(save_folder, filename)
    img_feats = torch.load(filepath, map_location='cpu', weights_only=True)
    return img_feats

def find_closest_points_per_cluster(x, cluster_ids, cluster_centers):
    # Dictionary to store the indices of the closest points for each cluster
    closest_points_idx_per_cluster = {cluster_id: [] for cluster_id in range(len(cluster_centers))}
    
    # Iterate over each cluster
    for cluster_id in range(len(cluster_centers)):
        # Filter points belonging to the current cluster
        indices_in_cluster = torch.where(cluster_ids == cluster_id)[0]
        points_in_cluster = x[indices_in_cluster]
        
        # Calculate distances from points in the cluster to the cluster center
        distances = torch.norm(points_in_cluster - cluster_centers[cluster_id], dim=1)

        if distances.numel() > 0:    
            
            # Find the index (within the cluster) of the point closest to the cluster center
            closest_idx_in_cluster = torch.argmin(distances).item()
            
            # Map back to the original index in x
            closest_global_idx = indices_in_cluster[closest_idx_in_cluster].item()
            
            # Store the global index
            closest_points_idx_per_cluster[cluster_id].append(closest_global_idx)

    return closest_points_idx_per_cluster

def launch():
    args = parse_args()
    pprint({k: v for k, v in vars(args).items() if k != 'api_key'})
    device = torch.device(args.device)
    torch.manual_seed(args.seed)

        # output
    makedir(args.output_base_path)
    output_path = os.path.join(args.output_base_path, args.output_filename)
    output_width_res_path = os.path.join(args.output_base_path,"width_res.json")
    frame_feat_path = args.frame_feat_path

    # resume
    processed = {}
    if not args.start_from_scratch and os.path.exists(output_path):
        processed = load_json(output_path)
        if 'data' in processed:
            processed = processed['data']
    if os.path.exists(output_width_res_path) and not args.start_from_scratch:
        width_previous = load_json(output_width_res_path)
    else:
        width_previous = []
    # A resumed relevance result is usable only when its cluster assignments exist.
    processed = {uid: item for uid, item in processed.items()
                 if uid in {row['name'] for row in width_previous}}

    # get input
    quids_to_exclude = set(list(processed.keys()))
    dataset = get_dataset(args, quids_to_exclude=quids_to_exclude, num_examples_to_run=args.num_examples_to_run)

    # configure prompt
    prompter = PromptFactory().get(args.prompt_type)

    # get model
    model = get_model(args)
    model.set_post_process_fn(prompter.post_process_fn)
    
    # save width expansion results
    all_width_res = width_previous

    # answer
    pbar = tqdm(total=len(dataset))
    for i, item in enumerate(dataset):
        ukey_1 = item['quid'] if 'quid' in item else item['uid']

        #init the cluster parameters
        tree_node = [0]
        max_cluster_num = args.max_cluster_num
        cluster_num = args.init_cluster_num
        iter_threshold = args.iter_threshold
        adaptive_rate = args.default_adpative_rate


        clip_length = int(1/args.fps) if args.fps < 1 else 1/args.fps
        few_shot_examples = build_fewshot_examples(args.fewshot_example_path, args.data_path)

        # load frame features
        frame_feats = load_frame_features(ukey_1, frame_feat_path).to(device)
        if isinstance(item['narration'], list) and len(frame_feats) != len(item['narration']):
            raise ValueError(f'{ukey_1}: frame features and captions must have the same count.')
        max_cluster_num = min(max_cluster_num, len(frame_feats))
        cluster_num = min(cluster_num, max_cluster_num)

        ### adaptive width expansion
        while(True):
            # width expansion
            cluster_ids_x, cluster_centers = kmeans(X=frame_feats, num_clusters=cluster_num, distance='cosine', device=device, seed=args.seed, tqdm_flag=False)
            # send cluster_ids_x to GPU 
            cluster_ids_x = cluster_ids_x.to(device)
            cluster_centers = cluster_centers.to(device)
            closest_points_idx_per_cluster = find_closest_points_per_cluster(frame_feats, cluster_ids_x, cluster_centers)
            if closest_points_idx_per_cluster is None:
                # print("closest_points_idx_per_cluster is None")
                continue
            representative_pairs = sorted((values[0], cid) for cid, values in closest_points_idx_per_cluster.items() if values)
            tree_node = [frame for frame, _ in representative_pairs]

            cluster_ids_x = cluster_ids_x.tolist()
            # relevance scoring
            model.set_post_process_fn(prompter.post_process_fn)
            prompt = prompter.fill(**item, fps=args.fps, clip_length=clip_length, num_words=args.num_words_in_sum, examplars=few_shot_examples, loc_pred = tree_node)
            prompt[-1] += f'\nThere are exactly {len(tree_node)} listed frames. Return exactly {len(tree_node)} relevance scores, one per listed frame in the same order.'
            # Local models can produce a valid list with the wrong length. Retry the
            # scoring request with the required count, rather than misaligning it.
            for scoring_attempt in range(3):
                try:
                    pred, info = model.forward(prompter.head, prompt)
                    if len(pred) == len(tree_node):
                        break
                    reason = f'Received {len(pred)} scores instead of {len(tree_node)}.'
                except ValueError as error:
                    reason = str(error)
                if scoring_attempt == 2:
                    raise ValueError(f'Invalid relevance scores for {ukey_1}: {reason}')
                print(f'Retrying relevance scoring: {reason}', flush=True)
                prompt[-1] += f'\nCorrection: {reason} Only return "frame relevance: [...]" containing exactly {len(tree_node)} integers from 1 to 3.'
            ukey_name = 'quid' if 'quid' in item else 'uid'

            # the output is the predicted frame relevance
            frame_relevance = pred
            if len(frame_relevance) != len(tree_node):
                raise ValueError(f'Expected {len(tree_node)} relevance scores, received {len(frame_relevance)}. Response: {info["response"]}')
            # The prompt lists frames in time order. Depth expansion indexes scores by cluster ID.
            cluster_relevance = [1] * cluster_num
            for (_, cid), score in zip(representative_pairs, frame_relevance):
                cluster_relevance[cid] = score
            high_relevance_frame_num = frame_relevance.count(3)

            if high_relevance_frame_num < iter_threshold:
                if cluster_num < max_cluster_num:
                    if adaptive_rate <= 1:
                        raise ValueError('Adaptive rate must be greater than 1.')
                    cluster_num = min(cluster_num * adaptive_rate, max_cluster_num)
                else:
                    break
            else:
                break
        all_width_res.append({"name": ukey_1, "tree_node": tree_node, "cluster_ids_x": cluster_ids_x})


        ukey = item[ukey_name]

        processed[ukey] = item

        processed[ukey]['prompt'] = prompt
        processed[ukey]['prompt_template'] = prompter.get_template_str()
        processed[ukey]['response'] = info['response']
        processed[ukey]['pred'] = pred
        processed[ukey]['cluster_relevance'] = cluster_relevance
        if args.save_info:
            processed[ukey]['info'] = {k: v for k, v in info.items() if k != 'response'}
        if i % args.save_every == 0:
            save_json(processed, output_path)
            save_json(all_width_res, output_width_res_path)
        pbar.update(1)

    save_json(all_width_res, output_width_res_path)

    # incorporate with backup prediction
    if len(args.backup_pred_path) > 0:
        backup = load_json(args.backup_pred_path)
        if 'data' in backup:
            backup = backup['data']
        for uid in processed:
            if processed[uid]['pred'] == -1:
                processed[uid]['pred'] = backup[uid]['pred']

    # if eval
    if not args.disable_eval and args.prompt_type != 'cap_score':
        if args.task == 'qa':
            if args.dataset == 'egoschema':
                processed = eval_qa_egoschema(processed)
            elif args.dataset in ['nextqa', 'intentqa', 'nextgqa']:
                processed = eval_qa_nextqa(args.anno_path, processed)
        elif args.task == 'gqa':
            if args.dataset == 'nextgqa':
                pred_qa_path = args.nextgqa_pred_qa_path if len(args.nextgqa_pred_qa_path) > 0 else None
                processed = eval_gqa(args.nextgqa_gt_ground_path, processed, pred_qa_path=pred_qa_path)
        elif args.task == 'sum':
            processed, sum_data = eval_sum(processed)
            save_json(sum_data, f'{Path(output_path).parent / Path(output_path).stem}_data.json')

    save_json(processed, output_path)


if __name__ == '__main__':
    launch()
