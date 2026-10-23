"""LLM backends shared by relevance scoring and final VideoTree QA."""
import os
from prompts import identity


def get_model(args):
    name = args.model
    if "qwen" in name.lower():
        return Qwen(name, args.temperature, args.device, args.max_new_tokens)
    if "gpt" in name.lower():
        return GPT(args.api_key, name, args.temperature, args.max_new_tokens)
    if "llama" in name.lower():
        return LLaMA(name, args.temperature, args.max_new_tokens)
    raise ValueError(f"Unsupported model: {name}")


class Model:
    def __init__(self):
        self.post_process_fn = identity

    def set_post_process_fn(self, fn):
        self.post_process_fn = fn


class GPT(Model):
    def __init__(self, api_key, model_name, temperature, max_new_tokens=768):
        super().__init__()
        from openai import OpenAI
        self.client = OpenAI(api_key=api_key or os.environ.get("OPENAI_API_KEY"),
                             timeout=120, max_retries=3)
        self.model_name = model_name
        self.temperature = temperature
        self.max_new_tokens = max_new_tokens

    def forward(self, head, prompts):
        messages = [{"role": "system", "content": head}]
        for prompt in prompts:
            messages.append({"role": "user", "content": prompt})
            response = self.client.chat.completions.create(
                model=self.model_name, messages=messages,
                temperature=self.temperature, max_tokens=self.max_new_tokens)
            text = response.choices[0].message.content
            messages.append({"role": "assistant", "content": text})
        info = response.usage.model_dump() if response.usage else {}
        info.update(response=text, message=messages)
        return self.post_process_fn(text), info


class Qwen(Model):
    """Use the local Qwen language model on captions, as in the original LLM stage."""
    def __init__(self, model_name, temperature, device="cuda", max_new_tokens=768):
        super().__init__()
        import torch
        from transformers import AutoTokenizer, AutoModelForImageTextToText
        if device == "cuda" and not torch.cuda.is_available():
            raise RuntimeError("CUDA is unavailable. Run this command in a PBS GPU job.")
        self.temperature = temperature
        self.max_new_tokens = max_new_tokens
        self.tokenizer = AutoTokenizer.from_pretrained(model_name)
        self.model = AutoModelForImageTextToText.from_pretrained(
            model_name, torch_dtype=torch.bfloat16 if device == "cuda" else torch.float32,
            device_map=device, attn_implementation="sdpa").eval()

    def forward(self, head, prompts):
        import torch
        messages = [{"role": "system", "content": head}]
        total_input = total_output = 0
        for prompt in prompts:
            messages.append({"role": "user", "content": prompt})
            inputs = self.tokenizer.apply_chat_template(
                messages, tokenize=True, add_generation_prompt=True,
                return_dict=True, return_tensors="pt").to(self.model.device)
            kwargs = {"max_new_tokens": self.max_new_tokens,
                      "do_sample": self.temperature > 0}
            if self.temperature > 0:
                kwargs["temperature"] = self.temperature
            if getattr(self,'stopping_criteria',None) is not None:
                kwargs['stopping_criteria']=self.stopping_criteria
            with torch.inference_mode():
                generated = self.model.generate(**inputs, **kwargs)
            tokens = generated[0, inputs.input_ids.shape[1]:]
            text = self.tokenizer.decode(tokens, skip_special_tokens=True).strip()
            total_input += inputs.input_ids.shape[1]
            total_output += len(tokens)
            messages.append({"role": "assistant", "content": text})
        info = {"response": text, "message": messages, "prompt_tokens": total_input,
                "completion_tokens": total_output, "total_tokens": total_input + total_output}
        return self.post_process_fn(text), info


class LLaMA(Model):
    def __init__(self, model_name, temperature, max_new_tokens=768):
        super().__init__()
        import torch
        from transformers import AutoTokenizer, pipeline
        tokenizer = AutoTokenizer.from_pretrained(model_name)
        tokenizer.pad_token = tokenizer.eos_token
        self.pipeline = pipeline("text-generation", model=model_name,
                                 torch_dtype=torch.float16, device_map="auto",
                                 tokenizer=tokenizer)
        self.max_new_tokens = max_new_tokens

    def forward(self, head, prompts):
        prompt = prompts[0]
        sequences = self.pipeline(prompt, do_sample=False, num_return_sequences=1,
                                  max_new_tokens=self.max_new_tokens, return_full_text=False)
        text = sequences[0]["generated_text"]
        return self.post_process_fn(text), {"message": prompt, "response": text}
