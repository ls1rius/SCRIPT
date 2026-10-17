"""Regression checks for frame evidence and hierarchy preservation."""
import unittest
import numpy as np
import torch
from string import Template
from prompts import PromptTemplate, first_char_as_answer, update_relevance_response, update_pred_response
from depth_expansion import hierarchical_clustering_with_external_primary, find_closest_points_in_temporal_order_subsub
from kmeans_pytorch import kmeans


class PipelineTests(unittest.TestCase):
    def test_first_frame_and_caption_boundaries(self):
        prompt = PromptTemplate('', [Template('$narration')], lambda x: x)
        text = prompt.fill(narration=['#C washes. Then dries.', '#O walks away'], loc_pred=[0], fps=1)[0]
        self.assertIn('Frame 0 (0s): #C washes. Then dries.', text)
        self.assertNotIn('walks away', text)
        with self.assertRaises(ValueError):
            prompt.fill(narration=['one'], loc_pred=[1], fps=1)

    def test_model_output_must_be_valid(self):
        self.assertEqual(first_char_as_answer('  B\n'), 1)
        self.assertEqual(first_char_as_answer(''), -1)
        self.assertEqual(update_pred_response('prediction:\nC'), 2)
        self.assertEqual(update_pred_response('cannot answer'), -1)
        self.assertEqual(update_relevance_response('Frame relevance:\n[1, 3, 2]'), [1, 3, 2])
        for text in ['not structured', 'frame relevance: [0, 3]']:
            with self.assertRaises(ValueError):
                update_relevance_response(text)

    def test_depth_keeps_singleton_clusters(self):
        features = torch.tensor([[1., 0., 0.], [0., 1., 0.], [0., 0., 1.]])
        labels = [0, 0, 1]
        scores = [3, 1]
        tree = hierarchical_clustering_with_external_primary(features, labels, scores, 4, 4)
        indices = find_closest_points_in_temporal_order_subsub(features, tree, scores)
        self.assertEqual(indices, [0, 1, 2])

    def test_cosine_kmeans_works_without_optional_soft_dtw(self):
        points = torch.tensor([[1., 0.], [0., 1.], [-1., 0.]])
        labels, centers = kmeans(points, 3, distance='cosine', seed=0, tqdm_flag=False)
        self.assertEqual(len(set(labels.tolist())), 3)
        self.assertTrue(torch.isfinite(centers).all())


if __name__ == '__main__':
    unittest.main()
