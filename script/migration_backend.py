"""Experiment-only worker: original VideoTree Qwen.forward and SCRIPT share weights."""
import json
import multiprocessing as mp
import time

from script_core.budget import ModelWorker


def worker(connection, cfg):
    from script_core.backend import LocalModels
    from model import Qwen
    from prompts import update_pred_response, identity
    from transformers import AutoTokenizer
    backend, qa = LocalModels(cfg), None
    while True:
        message = connection.recv()
        if message is None:
            return
        operation, inputs, deadline = message
        backend.deadline = deadline
        backend.model_pixels, backend.model_tokens = {}, {}
        started, cpu_started = time.monotonic(), time.process_time()
        try:
            if operation in ('videotree_qa', 'migration_guard'):
                backend.check_deadline()
                if qa is None:
                    model, _ = backend.load('vlm')
                    # Run the original, unmodified forward method on the same
                    # original tokenizer and generation settings, sharing weights.
                    qa = Qwen.__new__(Qwen)
                    qa.model = model
                    qa.tokenizer = AutoTokenizer.from_pretrained(cfg['models']['vlm'], local_files_only=True)
                    qa.temperature = 0
                    qa.max_new_tokens = cfg['models']['generation']['max_new_tokens']
                qa.set_post_process_fn(update_pred_response if operation == 'videotree_qa' else identity)
                prediction, info = qa.forward(inputs['head'], inputs['prompts'])
                backend.check_deadline()
                if operation == 'videotree_qa':
                    if prediction not in range(5):
                        raise ValueError('Original QA produced an illegal answer')
                    data = {'selected_option': str(prediction), 'text': info['response']}
                else:
                    text = info['response'].strip()
                    if text.startswith('```'):
                        text = text.split('\n', 1)[1].rsplit('```', 1)[0]
                    data = json.loads(text)
                result = {'data': data, 'usage': {'input_tokens': info['prompt_tokens'],
                          'output_tokens': info['completion_tokens']}, 'original_messages': info['message']}
            else:
                result = backend.call(operation, inputs)
            result.setdefault('usage', {}).update(cpu_seconds=time.process_time()-cpu_started,
                model_tensor_pixels=backend.model_pixels, input_tokens_by_model=backend.model_tokens)
            connection.send((True, result))
        except Exception as exc:
            connection.send((False, {'message': f'{type(exc).__name__}: {exc}',
                                     'usage': {'cpu_seconds': time.process_time()-cpu_started}}))


class MigrationWorker(ModelWorker):
    def start(self):
        if self.process is not None and self.process.is_alive():
            return
        ctx = mp.get_context('spawn')
        self.connection, child = ctx.Pipe()
        self.process = ctx.Process(target=worker, args=(child, self.cfg), daemon=True)
        self.process.start()
        child.close()
