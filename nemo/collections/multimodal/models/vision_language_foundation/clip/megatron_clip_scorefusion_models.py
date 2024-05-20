import torch
from torch import nn
import torch.nn.functional as F
import clip
import torch.distributed.nn
from omegaconf.dictconfig import DictConfig
from pytorch_lightning.accelerators import CPUAccelerator
from pytorch_lightning.trainer.trainer import Trainer
from tqdm import tqdm
import sys
sys.path.insert(0, "/NeMo/nemo")

from nemo.collections.multimodal.models.vision_language_foundation.clip.megatron_clip_models import MegatronCLIPModel
from nemo.collections.multimodal.models.vision_language_foundation.clip.megatron_clip_models import CLIPModel
from nemo.collections.multimodal.data.clip.clip_dataset import get_preprocess_fns
from nemo.collections.multimodal.losses.clip_loss import ClipLoss, InbatchContrastiveLoss
from nemo.collections.nlp.parts.utils_funcs import get_last_rank, torch_dtype_from_precision
from nemo.collections.vision.modules.vit.vit_backbone import VitBackbone
from nemo.core.classes.common import PretrainedModelInfo
from nemo.utils import logging
from nemo.collections.multimodal.data.clip.mbeir_dataset import (
    MBEIRMainDataset,
    MBEIRCandidatePoolDataset,
    MBEIRMainCollator,
    MBEIRCandidatePoolCollator,
    Mode,
    build_train_valid_datasets,
)
from torch.utils.data import DataLoader
from torch.utils.data import DistributedSampler

try:
    from apex.transformer.enums import AttnMaskType
    from apex.transformer.pipeline_parallel.utils import get_num_microbatches

    HAVE_APEX = True
except (ImportError, ModuleNotFoundError):
    HAVE_APEX = False


try:
    from megatron.core import parallel_state
    from megatron.core.pipeline_parallel.schedules import get_forward_backward_func

    HAVE_MEGATRON_CORE = True
except (ImportError, ModuleNotFoundError):

    HAVE_MEGATRON_CORE = False

import clip

class MegatronCLIPScoreFusionModel(MegatronCLIPModel):
    def __init__(self, cfg: DictConfig, trainer: Trainer, pre_process=True, post_process=True):
        super().__init__(cfg, trainer)

    #     #TODO add support for huggingface models instead of 
    #                           conversion to .nemo
       
        self.tokenizer = clip.tokenize
        

        # self.logit_scale = 

    def get_tokenizer(self):
        def tokenizer_wrapper(txt):
            tokenizer = self.tokenizer
            txt_tensor = tokenizer(txt, context_length=77, truncate=True)
            return txt_tensor

        return tokenizer_wrapper
    
    # TODO add dataset support 
    def setup(self, stage=None):
        """ PTL hook that is executed after DDP spawns.
            We setup datasets here as megatron datasets require DDP to instantiate.
            See https://pytorch-lightning.readthedocs.io/en/latest/common/lightning_module.html#setup for more information.
        Args:
            stage (str, optional): Can be 'fit', 'validate', 'test' or 'predict'. Defaults to None.
        """

        # log number of parameters
        if isinstance(self.model, list):
            num_parameters_on_device = sum(
                [sum([p.nelement() for p in model_module.parameters()]) for model_module in self.model]
            )
        else:
            num_parameters_on_device = sum([p.nelement() for p in self.model.parameters()])

        # to be summed across data parallel group
        total_num_parameters = torch.tensor(num_parameters_on_device).cuda()

        torch.distributed.all_reduce(total_num_parameters, group=parallel_state.get_model_parallel_group())

        logging.info(
            f'Pipeline model parallel rank: {parallel_state.get_pipeline_model_parallel_rank()}, '
            f'Tensor model parallel rank: {parallel_state.get_tensor_model_parallel_rank()}, '
            f'Number of model parameters on device: {num_parameters_on_device:.2e}. '
            f'Total number of model parameters: {total_num_parameters:.2e}.'
        )

        resume_checkpoint_path = self.trainer.ckpt_path
        if resume_checkpoint_path:
            init_consumed_samples = self._extract_consumed_samples_from_ckpt(resume_checkpoint_path)
        else:
            init_consumed_samples = 0
        self.init_consumed_samples = init_consumed_samples
        self.init_global_step = self.trainer.global_step

        self.build_train_valid_test_datasets()

        # Batch size need to be provided for dataset
        self._num_micro_batches = get_num_microbatches()
        self._micro_batch_size = self.cfg.micro_batch_size
        
        #training & validation datasets
        self.setup_training_data()

        # when using pipeline model parallel the final stage need to initialize word embeddings
        if parallel_state.get_pipeline_model_parallel_world_size() > 1:
            if isinstance(self.model, list):
                for i, module in enumerate(self.model):
                    parallel_state.set_virtual_pipeline_model_parallel_rank(i)
                parallel_state.set_virtual_pipeline_model_parallel_rank(0)
    
    def forward(self, batch):
    
        txt_batched = batch["txt_batched"]
        image_batched = batch["image_batched"]
        txt_mask_batched = batch["txt_mask_batched"]
        image_mask_batched = batch["image_mask_batched"]
        index_mapping = batch["index_mapping"]
        
#         output_tensor = self.model(image_batched, txt_batched)
#         image_features, text_features, logit_scale = output_tensor
        image_features = self.model.vision_encoder(image_batched)
        text_features = self.model.text_encoder(txt_batched)

        # Hugging face model directly called
        # image_features = self.model.encode_image(image_batched)
        # text_features = self.model.encode_text(txt_batched)
        
        embeddings = image_features * image_mask_batched.unsqueeze(-1) + text_features * txt_mask_batched.unsqueeze(-1)
        query_fused_embeds = embeddings[torch.tensor(index_mapping["query"]).flatten()]  
        pos_cand_fused_embeds = embeddings[torch.tensor(index_mapping["pos_cand"]).flatten()]  
        
        output_tensor = query_fused_embeds, pos_cand_fused_embeds
        return output_tensor

    def get_forward_output_and_loss_func(self):
        loss_func = InbatchContrastiveLoss(local_loss=self.cfg.local_loss, 
                                           gather_with_grad=self.cfg.gather_with_grad,
                                           enable_hard_neg=False) 

        def fwd_output_and_loss_func(dataloader_iter, model):
            batch = next(dataloader_iter)
            if parallel_state.get_pipeline_model_parallel_world_size() == 1:
                
                batch["txt_batched"] = batch["txt_batched"].to(device='cuda', non_blocking=True)
                batch["image_batched"] = batch["image_batched"].to(device='cuda', non_blocking=True)
                batch["txt_mask_batched"] =  batch["txt_mask_batched"].to(device='cuda', non_blocking=True)
                batch["image_mask_batched"] = batch["image_mask_batched"].to(device='cuda', non_blocking=True)

            else:
                # GPT3 uses only causal mask, which doesn't need attention mask
                if parallel_state.is_pipeline_first_stage():
                    # Fist pipeline stage needs only the tokens and position_ids
                    batch["txt_batched"] = batch["txt_batched"].to(device='cuda', non_blocking=True)
                    batch["image_batched"] = batch["image_batched"].to(device='cuda', non_blocking=True)
                    batch["txt_mask_batched"] =  batch["txt_mask_batched"].to(device='cuda', non_blocking=True)
                    batch["image_mask_batched"] = batch["image_mask_batched"].to(device='cuda', non_blocking=True)
                else:
                    # Intermediate / Last pipeline stage doesn't need any inputs
                    batch = None
            outputs = self.forward(batch)
            return outputs, loss_func

        return fwd_output_and_loss_func

    
    def build_train_valid_test_datasets(self):
        logging.info('Building datasets for CLIP Score Fusion...')
        if self.trainer.limit_val_batches > 1.0 and isinstance(self.trainer.limit_val_batches, float):
            raise ValueError("limit_val_batches must be an integer or float less than or equal to 1.0.")

        self._train_ds, self._validation_ds = build_train_valid_datasets(
            model_cfg=self.cfg, 
            tokenizer=self.tokenizer,
        )
        self._test_ds = None

        if self._train_ds is not None:
            logging.info(f'Length of train dataset: {len(self._train_ds)}')
        if self._validation_ds is not None:
            logging.info(f'Length of val dataset: {len(self._validation_ds)}')
        if self._test_ds is not None:
            logging.info(f'Length of test dataset: {len(self._test_ds)}')
        logging.info(f'Finished building datasets for CLIP Score Fusion.')

        return self._train_ds, self._validation_ds, self._test_ds
    
    
    
    def setup_training_data(self):

        train_collector = MBEIRMainCollator(
                        tokenizer=self.get_tokenizer(),
                        image_size=tuple(map(int, self.cfg.data_config.image_size.split(','))),
                        mode=Mode.TRAIN,
                        )
        
        train_sampler = DistributedSampler(
                        dataset=self._train_ds,
                        num_replicas=1,
                        rank=0,
                        shuffle=True,
                        )
        self._train_dl = DataLoader(dataset=self._train_ds,
                                    batch_size=self.cfg.dataloader_config.train_batch_size,
                                    num_workers=self.cfg.dataloader_config.num_workers,
                                    pin_memory=True,
                                    sampler=train_sampler,
                                    shuffle=False,  # Note: since we use sampler, shuffle should be False
                                    collate_fn=train_collector,
                                    drop_last=True,
                                    persistent_workers=True if self.cfg.dataloader_config.num_workers > 0 else False,
                                   )
        
        