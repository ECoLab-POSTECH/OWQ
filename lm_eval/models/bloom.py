import transformers
import torch
from lm_eval.base import BaseLM
from transformers import BloomForCausalLM, AutoTokenizer
import torch.nn.functional as F
from torch import nn
import torch
from tqdm import tqdm
import sys
sys.path.append('../../')

from owq.quant import *
from owq.recon import *
from owq.utils.misc import find_layers, check_arguments


class BLOOMLM(BaseLM):
    def __init__(
        self,
        model,
        batch_size=1,
        device=None,
        args='',
    ):

        super().__init__()

        if device == None:
            self._device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        else:
            self._device = device
        self.model_name = model
        self.batch_size_per_gpu = batch_size
        self.args = args

        self.model = BloomForCausalLM.from_pretrained(self.model_name, torch_dtype='auto')
        self.model.eval()
        self.seqlen = 2048

        # pretrained tokenizer for neo is broken for now so just hard-coding this to gpt2
        self.tokenizer = AutoTokenizer.from_pretrained(self.model_name, use_fast=False)

        self.vocab_size = self.tokenizer.vocab_size
        print('BLOOM vocab size: ', self.vocab_size)

    @property
    def eot_token_id(self):
        # we use EOT because end of *text* is more accurate for what we're doing than end of *sentence*
        return self.tokenizer.eos_token_id

    @property
    def max_length(self):
        return 2048
    @property
    def max_gen_toks(self):
        print('max_gen_toks fn')
        return 256

    @property
    def batch_size(self):
        # TODO: fix multi-gpu
        return self.batch_size_per_gpu  # * gpus

    @property
    def device(self):
        # TODO: fix multi-gpu
        return self._device

    def tok_encode(self, string: str):
        return self.tokenizer.encode(string, add_special_tokens=False)

    def tok_decode(self, tokens):
        return self.tokenizer.decode(tokens)

    def _model_call(self, inps):
        """
        inps: a torch tensor of shape [batch, sequence]
        the size of sequence may vary from call to call
        returns: a torch tensor of shape [batch, sequence, vocab] with the
        logits returned from the model
        """
        with torch.no_grad():
            return self.model(inps)[0][:, :, :250680]

    @torch.no_grad()
    def _model_logits_on_dataset(self, dataset_inps):
        dataset_logits = []
        nsamples = len(dataset_inps)

        dev = self.device

        model = self.model

        print('Evaluation...')

        use_cache = model.config.use_cache
        model.config.use_cache = False
        layers = model.transformer.h

        model.transformer.word_embeddings = model.transformer.word_embeddings.to(dev)
        model.transformer.word_embeddings_layernorm = model.transformer.word_embeddings_layernorm.to(dev)
        layers[0] = layers[0].to(dev)

        dtype = next(iter(model.parameters())).dtype
        inps = []
        outs = []

        for batch_idx, batch in enumerate(dataset_inps):
            inps.append(torch.zeros(
                (batch.shape[1], self.model.config.hidden_size), dtype=dtype,
            ))
            outs.append(torch.zeros(
                (batch.shape[1], self.model.config.hidden_size), dtype=dtype,
            ))

        cache = {'i': 0, 'attention_masks': [], 'alibis': []}

        class Catcher(nn.Module):
            def __init__(self, module):
                super().__init__()
                self.module = module

            def forward(self, inp, **kwargs):
                inps[cache['i']] = inp
                cache['i'] += 1
                cache['attention_masks'].append(kwargs['attention_mask'].detach().cpu())
                cache['alibis'].append(kwargs['alibi'].detach().cpu())
                raise ValueError

        layers[0] = Catcher(layers[0])
        for i in range(nsamples):
            batch = dataset_inps[i].to(dev)
            try:
                model(batch)
            except ValueError:
                pass
        layers[0] = layers[0].module

        layers[0] = layers[0].cpu()
        model.transformer.word_embeddings = model.transformer.word_embeddings.cpu()
        model.transformer.word_embeddings_layernorm = model.transformer.word_embeddings_layernorm.cpu()
        torch.cuda.empty_cache()

        attention_masks = cache['attention_masks']
        alibis = cache['alibis']

        for i in tqdm(range(len(layers))):
            layer = layers[i].to(dev)

            if self.args.nearest:
                subset = find_layers(layer)
                for name in subset:
                    quantizer = Quantizer()
                    quantizer.configure(
                        self.args.wbits, perchannel=True, sym=False, mse=False
                    )
                    W = subset[name].weight.data
                    quantizer.find_params(W, weight=True)
                    subset[name].weight.data = quantize(
                        W, quantizer.scale, quantizer.zero, quantizer.maxq
                    ).to(next(iter(layer.parameters())).dtype)

            for j in range(nsamples):
                outs[j] = layer(inps[j].to(self.device),
                      attention_mask=attention_masks[j].to(self.device),
                      alibi=alibis[j].to(self.device))[0].detach().cpu()

            layers[i] = layer.cpu()
            del layer
            torch.cuda.empty_cache()
            inps, outs = outs, inps

        model.transformer.ln_f = model.transformer.ln_f.to(dev)
        model.lm_head = model.lm_head.to(dev)

        for i in tqdm(range(nsamples), desc='Last Layer'):
            hidden_states = inps[i].unsqueeze(0).to(self.device)
            hidden_states = self.model.transformer.ln_f(hidden_states)
            batch_logits = F.log_softmax(self.model.lm_head(hidden_states)[0][:, :, :250680], dim=-1).cpu()
            dataset_logits.append(batch_logits)

        model.config.use_cache = use_cache
        return dataset_logits

    @torch.no_grad()
    def _model_logits_on_dataset2(self, dataset_inps):
        dataset_logits = []
        nbatches = len(dataset_inps)

        use_cache = self.model.config.use_cache
        self.model.config.use_cache = False
        layers = self.model.transformer.h

        self.model.transformer.word_embeddings = self.model.transformer.word_embeddings.to(self.device)
        self.model.transformer.word_embeddings_layernorm = self.model.transformer.word_embeddings_layernorm.to(
            self.device)
        layers[0] = layers[0].to(self.device)

        dtype = next(iter(self.model.parameters())).dtype


        inps = []
        outs = []
        for batch_idx, batch in enumerate(dataset_inps):
            inps.append(torch.zeros(
                (batch.shape[1], self.model.config.hidden_size), dtype=dtype,
            ))
            outs.append(torch.zeros(
                (batch.shape[1], self.model.config.hidden_size), dtype=dtype,
            ))

        cache = {'i': 0, 'attention_masks': [], 'alibi': []}

        class Catcher(nn.Module):
            def __init__(self, module):
                super().__init__()
                self.module = module

            def forward(self, inp, **kwargs):
                inps[cache['i']] = inp.cpu()
                cache['i'] += 1
                cache['attention_masks'].append(kwargs['attention_mask'].detach().cpu())
                cache['alibi'].append(kwargs['alibi'].detach().cpu())
                raise ValueError

        layers[0] = Catcher(layers[0])
        for i in range(nbatches):
            batch = dataset_inps[i].to(self.device)
            try:
                self.model(batch)
            except ValueError:
                pass
        layers[0] = layers[0].module

        layers[0] = layers[0].cpu()
        self.model.transformer.word_embeddings = self.model.transformer.word_embeddings.cpu()
        self.model.transformer.word_embeddings_layernorm = self.model.transformer.word_embeddings_layernorm.cpu()
        torch.cuda.empty_cache()  # TODO: maybe we don't need this?

        attention_masks = cache['attention_masks']
        alibis = cache['alibi']

        for i in range(len(layers)):
            print('layer: ', i)
            layer = layers[i].to(self.device)

            if self.args.wbits < 32 and self.args.nearest:
                subset = find_layers(layer)
                for name in subset:
                    if 'lm_head' in name:
                        continue
                    quantizer = Quantizer()
                    quantizer.configure(
                        self.args.wbits,
                        perchannel=True, sym=False, mse=False, norm=2.4
                    )
                    W = subset[name].weight.data
                    quantizer.find_params(W, weight=True)
                    subset[name].weight.data = quantize(
                        W, quantizer.scale, quantizer.zero, quantizer.maxq
                    ).to(next(iter(layer.parameters())).dtype)


            for j in range(nbatches):
                outs[j] = layer(inps[j].to(self.device),
                                attention_mask=attention_masks[j].to(self.device),
                                alibi=alibis[j].to(self.device))[0].detach().cpu()
            layers[i] = layer.cpu()
            del layer
            torch.cuda.empty_cache()
            inps, outs = outs, inps

        self.model.transformer.ln_f = self.model.transformer.ln_f.to(self.device)
        self.model.lm_head = self.model.lm_head.to(self.device)

        for i in tqdm(range(nbatches), desc='Last Layer'):
            hidden_states = inps[i].unsqueeze(0).to(self.device)
            hidden_states = self.model.transformer.ln_f(hidden_states)
            batch_logits = F.log_softmax(self.model.lm_head(hidden_states)[0][:, :, :250680], dim=-1).cpu()
            dataset_logits.append(batch_logits)

        return dataset_logits

    def _model_logits_on_dataset_2(self, inps):
        self.model = self.model.to(self.device)
        dataset_logits = []
        for batch in inps:
            multi_logits = F.log_softmax(
                self._model_call(batch), dim=-1
            ).cpu() # [batch, padding_length, vocab]
            dataset_logits.append(multi_logits)
        return dataset_logits


    def _model_generate(self, context, max_length, eos_token_id):
        return self.model.generate(
            context, max_length=max_length, eos_token_id=eos_token_id, do_sample=False
        )

# for backwards compatibility
BLOOM = BLOOMLM