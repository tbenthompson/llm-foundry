# Copyright 2022 MosaicML LLM Foundry authors
# SPDX-License-Identifier: Apache-2.0

import ast
import importlib
import json
import os
import tempfile
from argparse import ArgumentParser, Namespace
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import sentencepiece as spm
import torch
import transformers
from composer.utils import (get_file, maybe_create_object_store_from_uri,
                            parse_uri, safe_torch_load)
from transformers import (AutoConfig, AutoTokenizer, PretrainedConfig,
                          PreTrainedTokenizer)

from llmfoundry import MPTConfig, MPTForCausalLM


# TODO: maybe move this functionality to Composer
def get_hf_config_from_composer_state_dict(
        state_dict: Dict[str, Any]) -> PretrainedConfig:
    hf_config_dict = state_dict['state']['integrations']['huggingface'][
        'model']['config']['content']

    # Always set init_device='cpu'
    hf_config_dict['init_device'] = 'cpu'

    AutoConfig.register('mpt', MPTConfig)

    # backwards compatibility changes
    if hf_config_dict['model_type'] == 'mosaic_gpt':
        hf_config_dict['model_type'] = 'mpt'

    if 'attn_config' not in hf_config_dict:
        attn_config = {}
        attn_config['attn_type'] = 'multihead_attention'
        attn_config['attn_pdrop'] = hf_config_dict['attn_pdrop']
        del hf_config_dict['attn_pdrop']
        attn_config['attn_impl'] = hf_config_dict['attn_impl']
        del hf_config_dict['attn_impl']
        attn_config['qk_ln'] = hf_config_dict['attn_qk_ln']
        del hf_config_dict['attn_qk_ln']
        attn_config['clip_qkv'] = hf_config_dict['attn_clip_qkv']
        del hf_config_dict['attn_clip_qkv']
        attn_config['softmax_scale'] = hf_config_dict['softmax_scale']
        del hf_config_dict['softmax_scale']
        attn_config['prefix_lm'] = hf_config_dict['prefix_lm']
        del hf_config_dict['prefix_lm']
        attn_config['attn_uses_sequence_id'] = hf_config_dict[
            'attn_uses_sequence_id']
        del hf_config_dict['attn_uses_sequence_id']
        attn_config['alibi'] = hf_config_dict['alibi']
        del hf_config_dict['alibi']
        attn_config['alibi_bias_max'] = hf_config_dict['alibi_bias_max']
        del hf_config_dict['alibi_bias_max']

        hf_config_dict['attn_config'] = attn_config

    if 'init_config' not in hf_config_dict:
        init_config = {}

        init_config['name'] = hf_config_dict['param_init_fn']
        del hf_config_dict['param_init_fn']
        init_config['fan_mode'] = hf_config_dict['fan_mode']
        del hf_config_dict['fan_mode']
        init_config['init_nonlinearity'] = hf_config_dict['init_nonlinearity']
        del hf_config_dict['init_nonlinearity']
        init_config['init_gain'] = hf_config_dict['init_gain']
        del hf_config_dict['init_gain']
        init_config['init_std'] = hf_config_dict['init_std']
        del hf_config_dict['init_std']
        init_config['init_div_is_residual'] = hf_config_dict[
            'init_div_is_residual']
        del hf_config_dict['init_div_is_residual']
        init_config['emb_init_std'] = hf_config_dict['emb_init_std']
        del hf_config_dict['emb_init_std']
        init_config['emb_init_uniform_lim'] = hf_config_dict[
            'emb_init_uniform_lim']
        del hf_config_dict['emb_init_uniform_lim']

        hf_config_dict['init_config'] = init_config

    if 'mlp_ratio' in hf_config_dict:
        hf_config_dict['expansion_ratio'] = hf_config_dict['mlp_ratio']
        del hf_config_dict['mlp_ratio']

    if 'low_precision_layernorm' in hf_config_dict:
        if hf_config_dict['low_precision_layernorm']:
            hf_config_dict['norm_type'] = 'low_precision_layernorm'
        else:
            hf_config_dict['norm_type'] = 'layernorm'
        del hf_config_dict['low_precision_layernorm']

    return AutoConfig.for_model(**hf_config_dict)


# TODO: maybe move this functionality to Composer
def get_hf_tokenizer_from_composer_state_dict(
        state_dict: Dict[str, Any]) -> Optional[PreTrainedTokenizer]:
    hf_tokenizer_state = state_dict['state']['integrations']['huggingface'][
        'tokenizer']
    hf_tokenizer = None
    if hf_tokenizer_state != {}:
        with tempfile.TemporaryDirectory() as _tmp_dir:
            for filename, saved_content in hf_tokenizer_state.items():
                tokenizer_file_path = Path(
                    _tmp_dir) / f'{filename}{saved_content["file_extension"]}'
                if saved_content['file_extension'] == '.json':
                    with open(tokenizer_file_path, 'w') as _tmp_file:
                        json.dump(saved_content['content'], _tmp_file)
                elif saved_content['file_extension'] == '.txt':
                    with open(tokenizer_file_path, 'w') as _tmp_file:
                        for line in saved_content['content']:
                            _tmp_file.write(line)
                            _tmp_file.write('\n')
                elif saved_content['file_extension'] == '.model':
                    s = spm.SentencePieceProcessor()
                    s.load_from_serialized_proto(saved_content['content'])
                    with open(tokenizer_file_path, 'wb') as _tmp_file:
                        _tmp_file.write(s.serialized_model_proto())
            hf_tokenizer = AutoTokenizer.from_pretrained(_tmp_dir)

            # remove 'name_or_path'
            hf_tokenizer.name_or_path = ''
            hf_tokenizer.init_kwargs['name_or_path'] = ''

    return hf_tokenizer


def write_huggingface_pretrained_from_composer_checkpoint(
        checkpoint_path: Union[Path, str],
        output_path: Union[Path, str],
        output_precision: str = 'fp32',
        local_checkpoint_save_location: Optional[Union[Path,
                                                       str]] = None) -> None:
    """Convert a Composer checkpoint to a pretrained HF checkpoint folder.

    Write a ``config.json`` and ``pytorch_model.bin``, like
    :meth:`transformers.PreTrainedModel.from_pretrained` expects, from a
    composer checkpoint.

    .. note:: This function will not work properly if you used surgery algorithms when you trained your model. In that case you will want to
        load the model weights using the Composer :class:`~composer.Trainer` with the ``load_path`` argument.
    .. testsetup::
        import torch
        dataset = RandomTextClassificationDataset(size=16, use_keys=True)
        train_dataloader = torch.utils.data.DataLoader(dataset, batch_size=8)
        eval_dataloader = torch.utils.data.DataLoader(dataset, batch_size=8)
        import transformers
        from composer.models import HuggingFaceModel
        from composer.trainer import Trainer
        hf_model = transformers.AutoModelForSequenceClassification.from_pretrained('prajjwal1/bert-tiny', num_labels=2)
        hf_tokenizer = transformers.AutoTokenizer.from_pretrained('prajjwal1/bert-tiny')
        composer_model = HuggingFaceModel(hf_model, tokenizer=hf_tokenizer, metrics=[], use_logits=True)
        trainer = Trainer(model=composer_model,
                            train_dataloader=train_dataloader,
                            save_filename='composer-hf-checkpoint.pt',
                            max_duration='1ep',
                            save_folder='./')
        trainer.fit()
        trainer.close()

    Example:
    .. testcode::
        from composer.models import write_huggingface_pretrained_from_composer_checkpoint
        write_huggingface_pretrained_from_composer_checkpoint('composer-hf-checkpoint.pt', './hf-save-pretrained-output')
        loaded_model = transformers.AutoModelForSequenceClassification.from_pretrained('./hf-save-pretrained-output')

    Args:
        checkpoint_path (Union[Path, str]): Path to the composer checkpoint, can be a local path, or a remote path beginning with ``s3://``, or another backend
            supported by :meth:`composer.utils.maybe_create_object_store_from_uri`.
        output_path (Union[Path, str]): Path to the folder to write the output to. Can be a local path, or a remote path beginning with ``s3://``, or another backend
            supported by :meth:`composer.utils.maybe_create_object_store_from_uri`.
        output_precision (str, optional): The precision of the output weights saved to `pytorch_model.bin`. Can be one of ``fp32``, ``fp16``, or ``bf16``.
        local_checkpoint_save_location (Optional[Union[Path, str]], optional): If specified, where to save the checkpoint file to locally.
                                                                                If the input ``checkpoint_path`` is already a local path, this will be a symlink.
                                                                                Defaults to None, which will use a temporary file.
    """
    dtype = {
        'fp32': torch.float32,
        'fp16': torch.float16,
        'bf16': torch.bfloat16,
    }[output_precision]

    # default local path to a tempfile if path is not provided
    if local_checkpoint_save_location is None:
        tmp_dir = tempfile.TemporaryDirectory()
        local_checkpoint_save_location = Path(
            tmp_dir.name) / 'local-composer-checkpoint.pt'

    # create object store if output_path
    object_store = maybe_create_object_store_from_uri(str(output_path))
    if object_store is not None:
        local_output_path = tempfile.TemporaryDirectory().name
    else:
        local_output_path = output_path

    # create folder
    os.makedirs(local_output_path)

    # download the checkpoint file
    print(
        f'Downloading checkpoint from {checkpoint_path} -> {local_checkpoint_save_location}'
    )
    get_file(str(checkpoint_path), str(local_checkpoint_save_location))

    # Load the Composer checkpoint state dict
    print('Loading checkpoint into CPU RAM...')
    composer_state_dict = safe_torch_load(local_checkpoint_save_location)

    # Build and save HF Config
    print('#' * 30)
    print('Saving HF Model Config...')
    hf_config = get_hf_config_from_composer_state_dict(composer_state_dict)
    hf_config.torch_dtype = dtype
    hf_config.save_pretrained(local_output_path)
    print(hf_config)

    # Extract and save the HF tokenizer
    print('#' * 30)
    print('Saving HF Tokenizer...')
    hf_tokenizer = get_hf_tokenizer_from_composer_state_dict(
        composer_state_dict)
    if hf_tokenizer is not None:
        hf_tokenizer.save_pretrained(local_output_path)
        print(hf_tokenizer)
    else:
        print('Warning! No HF Tokenizer found!')

    # Extract the HF model weights
    print('#' * 30)
    print('Saving HF Model Weights...')
    weights_state_dict = composer_state_dict
    if 'state' in weights_state_dict:
        weights_state_dict = weights_state_dict['state']['model']
    torch.nn.modules.utils.consume_prefix_in_state_dict_if_present(
        weights_state_dict, prefix='model.')

    # Convert weights to desired dtype
    for k, v in weights_state_dict.items():
        if isinstance(v, torch.Tensor):
            weights_state_dict[k] = v.to(dtype=dtype)

    # Save weights
    torch.save(weights_state_dict,
               Path(local_output_path) / 'pytorch_model.bin')

    print('#' * 30)
    print(f'HF checkpoint folder successfully created at {local_output_path}.')

    if object_store is not None:
        print(
            f'Uploading HF checkpoint folder from {local_output_path} -> {output_path}'
        )
        for file in os.listdir(local_output_path):
            _, _, prefix = parse_uri(str(output_path))
            remote_file = os.path.join(prefix, file)
            local_file = os.path.join(local_output_path, file)
            object_store.upload_object(remote_file, local_file)
    print('Done.')
    print('#' * 30)


class DeleteSpecificNodes(ast.NodeTransformer):

    def __init__(self, nodes_to_remove: List[ast.AST]):
        self.nodes_to_remove = nodes_to_remove

    def visit(self, node: ast.AST):
        if node in self.nodes_to_remove:
            return None

        return super().visit(node)


def convert_to_relative_import(module_name: str) -> str:
    parts = module_name.split('.')
    return '.' + parts[-1]


def find_module_file(module_name: str) -> str:
    module = importlib.import_module(module_name)
    module_file = module.__file__
    return module_file


def process_file(file_path: str, folder_path: str) -> List[str]:
    with open(file_path, 'r') as f:
        source = f.read()

    tree = ast.parse(source)
    new_files_to_process = []
    nodes_to_remove = []
    for node in ast.walk(tree):
        # convert any llmfoundry imports into relative imports
        if isinstance(node,
                      ast.ImportFrom) and node.module.startswith('llmfoundry'):
            module_path = find_module_file(node.module)
            node.module = convert_to_relative_import(node.module)
            # recursively process any llmfoundry files
            new_files_to_process.append(module_path)
        # remove any imports from composer or omegaconf
        elif isinstance(
                node, ast.ImportFrom) and (node.module.startswith('composer') or
                                           node.module.startswith('omegaconf')):
            nodes_to_remove.append(node)
        # remove the Composer* class
        elif isinstance(node,
                        ast.ClassDef) and node.name.startswith('Composer'):
            nodes_to_remove.append(node)
        # remove the __all__ declaration in any __init__.py files, whose enclosing module
        # will be converted to a single file of the same name
        elif isinstance(node,
                        ast.Assign) and len(node.targets) == 1 and isinstance(
                            node.targets[0],
                            ast.Name) and node.targets[0].id == '__all__':
            nodes_to_remove.append(node)

    transformer = DeleteSpecificNodes(nodes_to_remove)
    new_tree = transformer.visit(tree)

    new_filename = os.path.basename(file_path)
    # special case for __init__.py to mimic the original submodule
    if new_filename == '__init__.py':
        new_filename = file_path.split('/')[-2] + '.py'
    new_file_path = os.path.join(folder_path, new_filename)
    with open(new_file_path, 'w') as f:
        f.write(ast.unparse(new_tree))

    return new_files_to_process


def edit_files_for_hf_compatibility(folder: str):
    files_to_process = [
        os.path.join(folder, filename)
        for filename in os.listdir(folder)
        if filename.endswith('.py')
    ]
    files_processed_and_queued = set(files_to_process)

    while len(files_to_process) > 0:
        to_process = files_to_process.pop()
        if os.path.isfile(to_process) and to_process.endswith('.py'):
            to_add = process_file(to_process, folder)
            for file in to_add:
                if file not in files_processed_and_queued:
                    files_to_process.append(file)
                    files_processed_and_queued.add(file)


def parse_args() -> Namespace:
    """Parse commandline arguments."""
    parser = ArgumentParser(
        description=
        'Convert Composer checkpoint and Omegaconf model config into a standard HuggingFace checkpoint folder, and optionally upload to the hub.'
    )
    parser.add_argument('--composer_path', type=str, required=True)
    parser.add_argument('--hf_output_path', type=str, required=True)
    parser.add_argument('--local_checkpoint_save_location',
                        type=str,
                        default=None)
    parser.add_argument('--output_precision',
                        type=str,
                        choices=['fp32', 'fp16', 'bf16'],
                        default='fp32')
    parser.add_argument('--hf_repo_for_upload', type=str, default=None)
    parser.add_argument('--test_uploaded_model', action='store_true')

    return parser.parse_args()


def main(args: Namespace) -> None:
    write_huggingface_pretrained_from_composer_checkpoint(
        checkpoint_path=args.composer_path,
        output_path=args.hf_output_path,
        output_precision=args.output_precision,
        local_checkpoint_save_location=args.local_checkpoint_save_location)

    dtype = {
        'fp32': torch.float32,
        'fp16': torch.float16,
        'bf16': torch.bfloat16,
    }[args.output_precision]

    # register config auto class
    MPTConfig.register_for_auto_class()

    # register model auto class
    MPTForCausalLM.register_for_auto_class('AutoModelForCausalLM')

    print(f'Loading model from {args.hf_output_path}')
    config = MPTConfig.from_pretrained(args.hf_output_path)
    # You have to edit the config this way, because attn_config is a nested dictionary
    config.attn_config['attn_impl'] = 'torch'
    loaded_hf_model = MPTForCausalLM.from_pretrained(args.hf_output_path,
                                                     config=config,
                                                     torch_dtype=dtype)
    delattr(loaded_hf_model.config, '_name_or_path')

    loaded_hf_model.save_pretrained(args.hf_output_path)

    print(f'Loading tokenizer from {args.hf_output_path}')
    tokenizer = transformers.AutoTokenizer.from_pretrained(args.hf_output_path)
    tokenizer.save_pretrained(args.hf_output_path)

    print('Editing files for HF compatibility...')
    edit_files_for_hf_compatibility(args.hf_output_path)

    if args.hf_repo_for_upload is not None:
        from huggingface_hub import HfApi
        api = HfApi()

        print(
            f'Uploading {args.hf_output_path} to HuggingFace Hub at {args.hf_repo_for_upload}'
        )
        api.create_repo(repo_id=args.hf_repo_for_upload,
                        use_auth_token=True,
                        repo_type='model',
                        private=True,
                        exist_ok=True)
        print('Repo created.')

        # ignore the full checkpoint file if we now have sharded checkpoint files
        ignore_patterns = []
        if any(
                f.startswith('pytorch_model-00001')
                for f in os.listdir(args.hf_output_path)):
            ignore_patterns.append('pytorch_model.bin')

        api.upload_folder(folder_path=args.hf_output_path,
                          repo_id=args.hf_repo_for_upload,
                          use_auth_token=True,
                          repo_type='model',
                          ignore_patterns=ignore_patterns)
        print('Folder uploaded.')

        if args.test_uploaded_model:
            print('Testing uploaded model...')
            hub_model = transformers.AutoModelForCausalLM.from_pretrained(
                args.hf_repo_for_upload,
                trust_remote_code=True,
                use_auth_token=True,
                torch_dtype=dtype)
            hub_tokenizer = transformers.AutoTokenizer.from_pretrained(
                args.hf_repo_for_upload,
                trust_remote_code=True,
                use_auth_token=True)

            assert sum(p.numel() for p in hub_model.parameters()) == sum(
                p.numel() for p in loaded_hf_model.parameters())
            assert all(
                str(type(module1)).split('.')[-2:] == str(type(module2)).split(
                    '.')[-2:] for module1, module2 in zip(
                        hub_model.modules(), loaded_hf_model.modules()))

            assert next(
                hub_model.parameters()
            ).dtype == dtype, f'Expected model dtype to be {dtype}, but got {next(hub_model.parameters()).dtype}'
            print(
                hub_tokenizer.batch_decode(
                    hub_model.generate(hub_tokenizer(
                        'MosaicML is', return_tensors='pt').input_ids,
                                       max_new_tokens=10)))


if __name__ == '__main__':
    main(parse_args())
