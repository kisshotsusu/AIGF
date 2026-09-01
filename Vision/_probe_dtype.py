import inspect, transformers
from transformers import Qwen3VLForConditionalGeneration
sig = inspect.signature(transformers.PreTrainedModel.from_pretrained)
params = list(sig.parameters)
print("has torch_dtype:", "torch_dtype" in params)
print("has dtype:", "dtype" in params)
