# Copyright (c) 2017-present, Facebook, Inc.
# All rights reserved.
#
# This source code is licensed under the license found in the LICENSE file in
# the root directory of this source tree. An additional grant of patent rights
# can be found in the PATENTS file in the same directory.

import math

from fairseq import utils

from fairseq.criterions import FairseqCriterion, register_criterion

import torch

@register_criterion('label_smoothed_cross_entropy_with_align')
class LabelSmoothedCrossEntropyCriterionWithAlign(FairseqCriterion):

    def __init__(self, args, task):
        super().__init__(args, task)
        self.eps = args.label_smoothing
        self.attn_loss_weight = args.attn_loss_weight

    @staticmethod
    def add_args(parser):
        """Add criterion-specific arguments to the parser."""
        # fmt: off
        parser.add_argument('--label-smoothing', default=0., type=float, metavar='D',
                            help='epsilon for label smoothing, 0 means no label smoothing')
        
        # SZH
        parser.add_argument('--attn-loss-weight', default=1., type=float, metavar='D',
                            help='weight of supervised attention loss')
        # fmt: on

    def forward(self, model, sample, reduce=True):
        """Compute the loss for the given sample.

        Returns a tuple with three elements:
        1) the loss
        2) the sample size, which is used as the denominator for the gradient
        3) logging outputs to display while training
        """
        net_output = model(**sample['net_input'])
        loss, nll_loss = self.compute_loss(model, net_output, sample, reduce=reduce)
        sample_size = sample['target'].size(0) if self.args.sentence_avg else sample['ntokens']
        logging_output = {
            'loss': utils.item(loss.data) if reduce else loss.data,
            'nll_loss': utils.item(nll_loss.data) if reduce else nll_loss.data,
            'ntokens': sample['ntokens'],
            'nsentences': sample['target'].size(0),
            'sample_size': sample_size,
        }
        return loss, sample_size, logging_output

    def compute_loss(self, model, net_output, sample, reduce=True):
        # T: target_loss
        lprobs = model.get_normalized_probs(net_output, log_probs=True) # BaseFairseqModel.get_normalized_probs, softmax / log_softmax, (B,T,ClassNum)
        lprobs = lprobs.view(-1, lprobs.size(-1)) # (B*T, ClassNum) 
        target = model.get_targets(sample, net_output).view(-1, 1) #BaseFairseqModel.get.. Get sample target or netoutput. Sample['target'] (B*T, 1)
        non_pad_mask = target.ne(self.padding_idx) #(B*T, 1) # 是否是padding的矩阵，padding为0，元素为1
        nll_loss = -lprobs.gather(dim=-1, index=target)[non_pad_mask] #所有类别中选择正确类别的改率 #Vector : (B*T(Remove Padding))
        smooth_loss = -lprobs.sum(dim=-1, keepdim=True)[non_pad_mask]
        
        
        if not ('word_ids' in sample.keys() and 'attns' in net_output[1].keys()):
            attn_loss = torch.zeros(nll_loss.size())
        else:
            #输出内容里有att矩阵
            attns = net_output[1]['attns']
            src_len = attns[0].size()[2]
            tgt_len = attns[0].size()[1]
            
            # 生成Attn Ground Truth
            source_word_ids = sample['word_ids']['source_word_ids']
            target_word_ids = sample['word_ids']['target_word_ids']
            # word_is为第几个token pair, 例如0 0 1 1 1 2 3 3 4 4 4 4 5 5，两个序列里相同id的为对应的pair
            s = source_word_ids.unsqueeze(1).repeat(1,tgt_len,1) #(B, S) -> (B,1,S) -> (B,T,S)
            t = target_word_ids.unsqueeze(2).repeat(1,1,src_len) #(B, T) -> (B,T,1) -> (B,T,S)
            word_attn = torch.eq(s,t).float()  #(B,T,S) #
            # Normalize word_attn
            attn_word_num = torch.sum(word_attn, dim=-1, keepdim=True) #(B,T,1)
            mx = torch.max(attn_word_num)
            attn_word_num = torch.clamp(attn_word_num,1,mx)
            true_word_attn = word_attn / attn_word_num #(B,T,S) 比如当前这个target token对应三个source token,则每个0.33
            
            # Sentence_Normalize
            # sent_ids类似word ids，相同id的为同一个句子，处理方式和word ids类似
            source_sent_ids = sample['net_input']['source_sent_ids']
            target_sent_ids = sample['net_input']['target_sent_ids']
            s = source_sent_ids.unsqueeze(1).repeat(1,tgt_len,1) #(B, S) -> (B,1,S) -> (B,T,S)
            t = target_sent_ids.unsqueeze(2).repeat(1,1,src_len) #(B, T) -> (B,T,1) -> (B,T,S)
            sent_mask = torch.eq(s,t).float() #(B, T, S)
            sent_word_num = torch.sum(sent_mask, dim=-1, keepdim=True) #(B,T,1)
            mx = torch.max(sent_word_num)
            sent_word_num = torch.clamp(sent_word_num,1,mx)
            sent_word_weight = sent_mask / sent_word_num
            
            #计算每层att的损失
            attn_loss_each_layer = []
            for attn in attns:
                attn_loss_layer = attn - true_word_attn #(B,T,S)
                attn_loss_layer = attn_loss_layer.pow(2)
                attn_loss_layer = torch.mul(attn_loss_layer,sent_word_weight) #(B,T,S)
                attn_loss_layer = torch.sum(attn_loss_layer, dim=-1,keepdim=True) # weight_sum, #(B,T,1)
                attn_loss_layer = attn_loss_layer.view(-1,1) #(B*T,1)
                attn_loss_each_layer.append(attn_loss_layer)
            
            attn_loss_average_all_layer = torch.mean(torch.stack(attn_loss_each_layer),dim=0) #(B*T,1)
            attn_loss = attn_loss_average_all_layer[non_pad_mask] #(B*T(Remove Padding))
        
        if reduce:
            nll_loss = nll_loss.sum()
            smooth_loss = smooth_loss.sum()
            attn_loss = attn_loss.sum()
        eps_i = self.eps / lprobs.size(-1)
        loss = (1. - self.eps) * nll_loss + eps_i * smooth_loss + self.attn_loss_weight * attn_loss
        return loss, nll_loss

    @staticmethod
    def aggregate_logging_outputs(logging_outputs):
        """Aggregate logging outputs from data parallel training."""
        ntokens = sum(log.get('ntokens', 0) for log in logging_outputs)
        nsentences = sum(log.get('nsentences', 0) for log in logging_outputs)
        sample_size = sum(log.get('sample_size', 0) for log in logging_outputs)
        return {
            'loss': sum(log.get('loss', 0) for log in logging_outputs) / sample_size / math.log(2),
            'nll_loss': sum(log.get('nll_loss', 0) for log in logging_outputs) / ntokens / math.log(2),
            'ntokens': ntokens,
            'nsentences': nsentences,
            'sample_size': sample_size,
        }