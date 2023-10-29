from os.path import join
import sys


sys.path += ['../']
sys.path += ['../../']
import argparse
import glob
import json
import logging
import os
import random
import numpy as np
import torch
import copy

sys.path.append(os.getcwd())
sys.path.append(os.path.abspath(os.path.dirname(os.getcwd())))
#  
from torch.utils.data import DataLoader, RandomSampler, SequentialSampler
from torch.utils.data.distributed import DistributedSampler
from tqdm import tqdm
import torch.distributed as dist
from torch import nn
import torch.nn.functional as F
from model.models import BiBertEncoder, ColBERT,  HFBertEncoder, Reranker
from model.models import CrossBERTKDLoss, BiEncoderKDLoss, ColBERTKDLoss, ColBERTNllLoss
import random
from transformers import (
    BertTokenizer,
    get_linear_schedule_with_warmup,
)
from transformers import glue_processors as processors
from torch import nn
try:
    from torch.utils.tensorboard import SummaryWriter
except ImportError:
    from tensorboardX import SummaryWriter
import pandas as pd

logger = logging.getLogger(__name__)
from utils.util import (
    set_seed,
    is_first_worker,
    TraditionDataset
)
from utils.dpr_utils import (
    load_states_from_checkpoint,
    get_model_obj,
    CheckpointState,
    get_optimizer,
    all_gather_list
)
import collections
from torch.nn.utils.rnn import pad_sequence
from utils.marco_until import (
    Rocketqa_v2Dataset
)

retrieverBatch = collections.namedtuple(
    "BiENcoderInput",
    [
        "q_ids",
        "q_attn_mask",
        "c_ids",
        "c_attn_mask",
        "c_q_mapping",
        "is_positive",
    ],
)



def train(args, double_teacher, teacher_model, model, tokenizer):
    """ Train the model """
    logger.info("Training/evaluation parameters %s", args)
    tb_writer = None
    if is_first_worker():
        tb_writer = SummaryWriter(log_dir=args.log_dir)

    args.train_batch_size = args.per_gpu_train_batch_size * max(1, args.n_gpu)  # nll loss for query
    optimizer = get_optimizer(args, model, lr=args.learning_rate, weight_decay=args.weight_decay)

    if args.open_LwF:
        logger.info("***** copy student model to Stable Distillation *****")
        student_copy = copy.deepcopy(model)

    if args.fp16:
        try:
            from apex import amp
        except ImportError:
            raise ImportError("Please install apex from https://www.github.com/nvidia/apex to use fp16 training.")
        model, optimizer = amp.initialize(model, optimizer, opt_level=args.fp16_opt_level)

    if args.teacher_step:
        teacher_optimizer = get_optimizer(args, teacher_model, lr=args.teacher_learning_rate, weight_decay=args.weight_decay)

    # Distributed training (should be after apex fp16 initialization)
    if args.local_rank != -1:
        # from apex.parallel import DistributedDataParallel as DDP
        model = torch.nn.parallel.DistributedDataParallel(
            model, device_ids=[args.rank], output_device=args.rank, find_unused_parameters=False,
        )
        teacher_model = torch.nn.parallel.DistributedDataParallel(
            teacher_model, device_ids=[args.rank], output_device=args.rank, find_unused_parameters=False,
        )
        if double_teacher is not None:
            double_teacher = torch.nn.parallel.DistributedDataParallel(
                double_teacher, device_ids=[args.rank], output_device=args.rank, find_unused_parameters=False,
            )
        if args.open_LwF:
            student_copy = torch.nn.parallel.DistributedDataParallel(
                student_copy, device_ids=[args.rank], output_device=args.rank, find_unused_parameters=False,
            )
    
    # Train!
    logger.info("***** Running training *****")
    logger.info("  Max steps = %d", args.max_steps)
    logger.info("  Instantaneous batch size per GPU = %d", args.per_gpu_train_batch_size)
    logger.info(
        "  Total train batch size (w. parallel, distributed & accumulation) = %d",
        args.train_batch_size
        * args.gradient_accumulation_steps
        * (torch.distributed.get_world_size() if args.local_rank != -1 else 1),
    )
    logger.info("  Gradient Accumulation steps = %d", args.gradient_accumulation_steps)

    tr_loss = 0.0
    tr_distll_loss = 0.0
    tr_contr_loss = 0.0

    teacher_count = 0
    double_teacher_count = 0


    model.zero_grad()
    model.train()
    if args.teacher_step:
        teacher_model.zero_grad()
        teacher_model.train()
    set_seed(args)  # Added here for reproductibility
    iter_count = 0

    scheduler = get_linear_schedule_with_warmup(
        optimizer, num_warmup_steps=args.warmup_steps, num_training_steps=args.max_steps
    )
    if args.teacher_step:
        teacher_scheduler = get_linear_schedule_with_warmup(
            teacher_optimizer, num_warmup_steps=args.warmup_steps, num_training_steps=args.max_steps
        )

    global_step = 0
    if args.neg_type == 'random':
        # train_dataset = Rocketqa_v2Dataset(args.origin_data_dir, tokenizer, num_hard_negatives=args.number_neg,
        #                                max_seq_length=args.max_seq_length, corpus_path=args.corpus_path, is_training=True)
        train_dataset = TraditionDataset(args.origin_data_dir, tokenizer, num_hard_negatives=args.number_neg,
                                         num_easy_negatives=args.number_easy_neg,
                                         max_seq_length=args.max_seq_length, shuffle_positives=args.shuffle_positives, is_training=True)
    elif args.neg_type == 'descend':
        # train_dataset = Rocketqa_v2Dataset(args.origin_data_dir, tokenizer, num_hard_negatives=args.number_neg,
        #                                    max_seq_length=args.max_seq_length, corpus_path=args.corpus_path, is_training=False)
        train_dataset = TraditionDataset(args.origin_data_dir, tokenizer, num_hard_negatives=args.number_neg,
                                         num_easy_negatives=args.number_easy_neg,
                                         max_seq_length=args.max_seq_length, shuffle_positives=args.shuffle_positives, is_training=False)
    else:
        logger.info("no such type of neg type...")
        exit(0)
    # train_sample = RandomSampler(train_dataset) if args.local_rank == -1 else DistributedSampler(train_dataset)
    # train_dataloader = DataLoader(train_dataset, sampler=train_sample,
    #                               collate_fn=Rocketqa_v2Dataset.get_collate_fn(args),
    #                               batch_size=args.train_batch_size, num_workers=10)

    train_sample = RandomSampler(train_dataset) if args.local_rank == -1 else DistributedSampler(train_dataset)
    train_dataloader = DataLoader(train_dataset, sampler=train_sample,
                                  collate_fn=TraditionDataset.get_collate_fn(args),
                                  batch_size=args.train_batch_size, num_workers=10)

    # load last checkpoint
    if args.output_dir is not None:
        checkpoint_files = []
        if os.path.exists(args.output_dir):
            for item in os.scandir(args.output_dir):
                if item.is_file():
                    if "checkpoint" in item.path:
                        checkpoint_files.append(item.path)
            if len(checkpoint_files) != 0:
                checkpoint_files.sort(key=lambda f: int(f.split('checkpoint-')[1]), reverse=True)
                logger.info("***** load " + checkpoint_files[0] + " *****")
                saved_state = load_states_from_checkpoint(checkpoint_files[0])
                global_step = _load_saved_state(model, optimizer, scheduler, saved_state)
            else:
                logger.info("***** there are no checkpoint in" + args.output_dir + " *****")

    #validate_rank = evaluate_dev(args, model, tokenizer)[0]
    #print(validate_rank)
    while global_step < args.max_steps:
        epoch_iterator = tqdm(train_dataloader, desc="Iteration", disable=args.local_rank not in [-1, 0])
        if args.num_epoch != 0 and iter_count > args.num_epoch:
            break
        # train_dataset = load_stream_dataset(args)

        for step, batch in enumerate(epoch_iterator):
            model.train()

            batch_retriever = batch['retriever']
            inputs_retriever = {"query_ids": batch_retriever[0].long().to(args.device),
                            "attention_mask_q": batch_retriever[1].long().to(args.device),
                            "input_ids_a": batch_retriever[2].long().to(args.device),
                            "attention_mask_a": batch_retriever[3].long().to(args.device)}
            local_positive_idxs = batch_retriever[4]

            batch_reranker = tuple(t.to(args.device) for t in batch['reranker'])
            inputs_reranker = {"input_ids": batch_reranker[0].long(), "attention_mask": batch_reranker[1].long()}

            model.train()

            if args.teacher_type == 'dual_encoder':
                if args.double_teacher_type == 'cross_encoder':
                    local_q_vector, local_ctx_vectors = model(**inputs_retriever)
                    with torch.no_grad():
                        local_teacher_q_vector, local_teacher_ctx_vectors = teacher_model(**inputs_retriever)
                    with torch.no_grad():
                        binary_logits, relevance_logits, _ = double_teacher(**inputs_reranker)
                    loss_function1 = BiEncoderKDLoss()
                    teacher1_loss, teacher1_is_correct = loss_function1.calc(
                        args,
                        local_q_vector,
                        local_ctx_vectors,
                        local_teacher_q_vector,
                        local_teacher_ctx_vectors,
                        local_positive_idxs,
                    )
                    loss_function2 = CrossBERTKDLoss()
                    teacher2_loss, teacher2_is_correct = loss_function2.calc(
                        args,
                        local_q_vector,
                        local_ctx_vectors,
                        relevance_logits,
                    )
                    if teacher1_loss.item() >= teacher2_loss.item():
                        loss = teacher1_loss
                        is_correct = teacher1_is_correct
                        teacher_count += 1
                    else:
                        loss = teacher2_loss
                        is_correct = teacher2_is_correct
                        double_teacher_count += 1

                else:
                    # student inference
                    local_q_vector, local_ctx_vectors = model(**inputs_retriever)
                    with torch.no_grad():
                        # teacher inference
                        local_teacher_q_vector, local_teacher_ctx_vectors = teacher_model(**inputs_retriever)

                    if args.off_inbatch:
                        logger.info("in-batch neg is cancel...")
                        loss_function = BiEncoderKDLoss()
                        loss, is_correct = loss_function.calc(
                            args,
                            local_q_vector,
                            local_ctx_vectors,
                            local_teacher_q_vector,
                            local_teacher_ctx_vectors,
                            local_positive_idxs,
                        )
                    else:
                        loss, is_correct = caculate_cont_loss(args, local_q_vector, local_ctx_vectors,
                                                              local_teacher_q_vector, local_teacher_ctx_vectors,
                                                              local_positive_idxs)
            elif args.teacher_type == 'ColBERT':
                if args.ts_share_weight:
                    local_q_vector, local_ctx_vectors, _, _ = model(**inputs_retriever)
                    _, _, local_teacher_q_hidden, local_teacher_ctx_hidden = teacher_model(**inputs_retriever)
                    loss, is_correct = caculate_Col_loss(args, local_q_vector, local_ctx_vectors,
                                                         local_teacher_q_hidden, local_teacher_ctx_hidden,
                                                         local_positive_idxs)
                elif args.teacher_step:
                    # teacher step
                    _, _, local_teacher_q_hidden, local_teacher_ctx_hidden = teacher_model(**inputs_retriever)
                    teacher_loss, teacher_is_correct = caculate_Col_NLLloss(args, local_teacher_q_hidden,
                                                                         local_teacher_ctx_hidden,
                                                                            local_positive_idxs)
                    teacher_loss.backward()
                    torch.nn.utils.clip_grad_norm_(teacher_model.parameters(), args.max_grad_norm)
                    teacher_optimizer.step()
                    teacher_scheduler.step()
                    teacher_model.zero_grad()
                    # student step
                    with torch.no_grad():
                        _, _, local_teacher_q_hidden, local_teacher_ctx_hidden = teacher_model(**inputs_retriever)
                    local_q_vector, local_ctx_vectors = model(**inputs_retriever)
                    loss, is_correct = caculate_Col_loss(args, local_q_vector, local_ctx_vectors,
                                                         local_teacher_q_hidden, local_teacher_ctx_hidden,
                                                         local_positive_idxs)
                else:
                    local_q_vector, local_ctx_vectors = model(**inputs_retriever)
                    with torch.no_grad():
                        _, _, local_teacher_q_hidden, local_teacher_ctx_hidden = teacher_model(**inputs_retriever)
                    loss, is_correct = caculate_Col_loss(args, local_q_vector, local_ctx_vectors,
                                                          local_teacher_q_hidden, local_teacher_ctx_hidden,
                                                         inputs_retriever['attention_mask_a'], local_positive_idxs)
            elif args.teacher_type == "cross_encoder":
                teacher_model.eval()
                local_q_vector, local_ctx_vectors = model(**inputs_retriever)
                with torch.no_grad():
                    binary_logits, relevance_logits, _ = teacher_model(**inputs_reranker)

                if args.open_LwF:
                    student_copy.eval()
                    ori_q_vector, ori_ctx_vectors = student_copy(**inputs_retriever)
                    loss_function = CrossBERTKDLoss()
                    loss, is_correct = loss_function.calc(
                        args,
                        local_q_vector,
                        local_ctx_vectors,
                        relevance_logits,
                        LwF=True,
                        ori_q_vector=ori_q_vector,
                        ori_ctx_vectors=ori_ctx_vectors,
                    )
                else:
                    loss_function = CrossBERTKDLoss()
                    loss, is_correct = loss_function.calc(
                        args,
                        local_q_vector,
                        local_ctx_vectors,
                        relevance_logits,
                    )

            else:
                logger.info("no such type of teacher model " + args.teacher_type)
                exit(0)

            # loss,is_correct = caculate_cont_loss(args, local_q_vector, local_ctx_vectors, local_teacher_q_vector, local_teacher_ctx_vectors, local_positive_idxs)

            # if args.teacher_step:
            #     teacher_loss.backward()
            #     torch.nn.utils.clip_grad_norm_(teacher_model.parameters(), args.max_grad_norm)
            #     teacher_optimizer.step()
            #     teacher_scheduler.step()
            #     teacher_model.zero_grad()

            loss = loss / args.gradient_accumulation_steps
            if args.fp16:
                with amp.scale_loss(loss, optimizer) as scaled_loss:
                    scaled_loss.backward()
            else:
                loss.backward()

            tr_loss += loss.item()
            if (step + 1) % args.gradient_accumulation_steps == 0:
                if args.fp16:
                    torch.nn.utils.clip_grad_norm_(amp.master_params(optimizer), args.max_grad_norm)
                else:
                    torch.nn.utils.clip_grad_norm_(model.parameters(), args.max_grad_norm)

                optimizer.step()
                scheduler.step()
                model.zero_grad()
                global_step += 1

                if args.logging_steps > 0 and global_step % args.logging_steps == 0:
                    logs = {}
                    loss_scalar = tr_loss / args.logging_steps
                    learning_rate_scalar = scheduler.get_last_lr()[0]
                    logs["learning_rate"] = learning_rate_scalar
                    logs["loss"] = loss_scalar
                    if double_teacher is not None:
                        logs["t1_step"] = teacher_count
                        logs["t2_step"] = double_teacher_count
                    tr_loss = 0
                    if is_first_worker():
                        for key, value in logs.items():
                            tb_writer.add_scalar(key, value, global_step)
                        logger.info(json.dumps({**logs, **{"step": global_step}}))

                if args.save_steps > 0 and global_step % args.save_steps == 0:
                    # if global_step > 500000:
                    #     validate_rank = evaluate_dev(args, model, tokenizer)
                    # else:
                    #     validate_rank = evaluate_dev(args, model, tokenizer)[0]
                    if is_first_worker():
                        _save_checkpoint(args, model, optimizer, scheduler, global_step)
                        # tb_writer.add_scalar("dev_nll_loss/dev_avg_rank", validate_rank, global_step)
                if global_step >= args.max_steps:
                    break
    if args.local_rank == -1 or torch.distributed.get_rank() == 0:
        tb_writer.close()
    return global_step

'''
large dual encoder -> small dual encoder
'''
def caculate_cont_loss(args, local_q_vector, local_ctx_vectors, local_teacher_q_vector, local_teacher_ctx_vectors, local_positive_idxs):
    if torch.distributed.get_world_size() > 1:
        q_vector_to_send = (
            torch.empty_like(local_q_vector).cpu().copy_(local_q_vector).detach_()
        )
        ctx_vector_to_send = (
            torch.empty_like(local_ctx_vectors).cpu().copy_(local_ctx_vectors).detach_()
        )

        teacher_q_vector_to_send = (
            torch.empty_like(local_teacher_q_vector).cpu().copy_(local_teacher_q_vector).detach_()
        )
        teacher_ctx_vector_to_send = (
            torch.empty_like(local_teacher_ctx_vectors).cpu().copy_(local_teacher_ctx_vectors).detach_()
        )

        global_question_ctx_vectors = all_gather_list(
            [
                q_vector_to_send,
                ctx_vector_to_send,
                teacher_q_vector_to_send,
                teacher_ctx_vector_to_send,
                local_positive_idxs,
            ],
            max_size=640000000,
        )

        global_q_vector = []
        global_ctxs_vector = []
        global_teacher_q_vector = []
        global_teacher_ctxs_vector = []

        # ctxs_per_question = local_ctx_vectors.size(0)
        positive_idx_per_question = []
        # hard_negatives_per_question = []

        total_ctxs = 0

        for i, item in enumerate(global_question_ctx_vectors):
            q_vector, ctx_vectors, teacher_q_vector, teacher_ctxs_vector, positive_idx = item

            if i != args.local_rank:
                global_q_vector.append(q_vector.to(local_q_vector.device))
                global_ctxs_vector.append(ctx_vectors.to(local_q_vector.device))
                global_teacher_q_vector.append(teacher_q_vector.to(local_q_vector.device))
                global_teacher_ctxs_vector.append(teacher_ctxs_vector.to(local_q_vector.device))
                positive_idx_per_question.extend([v + total_ctxs for v in positive_idx])
            else:
                global_q_vector.append(local_q_vector)
                global_ctxs_vector.append(local_ctx_vectors)
                global_teacher_q_vector.append(local_teacher_q_vector)
                global_teacher_ctxs_vector.append(local_teacher_ctx_vectors)
                positive_idx_per_question.extend(
                    [v + total_ctxs for v in local_positive_idxs]
                )
            total_ctxs += ctx_vectors.size(0)
        global_q_vector = torch.cat(global_q_vector, dim=0)
        global_ctxs_vector = torch.cat(global_ctxs_vector, dim=0)
        global_teacher_q_vector = torch.cat(global_teacher_q_vector, dim=0)
        global_teacher_ctxs_vector = torch.cat(global_teacher_ctxs_vector, dim=0)
    else:
        global_q_vector = local_q_vector
        global_ctxs_vector = local_ctx_vectors
        global_teacher_q_vector = local_teacher_q_vector
        global_teacher_ctxs_vector = local_teacher_ctx_vectors
        positive_idx_per_question = local_positive_idxs

    loss_function = BiEncoderKDLoss()
    loss, is_correct = loss_function.calc(
        args,
        global_q_vector,
        global_ctxs_vector,
        global_teacher_q_vector,
        global_teacher_ctxs_vector,
        positive_idx_per_question,
    )
    return loss, is_correct

def caculate_Col_loss(args, local_q_vector, local_ctx_vectors, local_teacher_q_hidden, local_teacher_ctx_hidden, local_teacher_ctx_mask, local_positive_idxs):
    if torch.distributed.get_world_size() > 1:
        q_vector_to_send = (
            torch.empty_like(local_q_vector).cpu().copy_(local_q_vector).detach_()
        )
        ctx_vector_to_send = (
            torch.empty_like(local_ctx_vectors).cpu().copy_(local_ctx_vectors).detach_()
        )
        teacher_q_hidden_to_send = (
            torch.empty_like(local_teacher_q_hidden).cpu().copy_(local_teacher_q_hidden).detach_()
        )
        teacher_ctx_hidden_to_send = (
            torch.empty_like(local_teacher_ctx_hidden).cpu().copy_(local_teacher_ctx_hidden).detach_()
        )
        teacher_ctx_mask_to_send = (
            torch.empty_like(local_teacher_ctx_mask).cpu().copy_(local_teacher_ctx_mask).detach_()
        )

        global_question_ctx_vectors = all_gather_list(
            [
                q_vector_to_send,
                ctx_vector_to_send,
                teacher_q_hidden_to_send,
                teacher_ctx_hidden_to_send,
                teacher_ctx_mask_to_send,
                local_positive_idxs,
            ],
            max_size=640000000,
        )

        global_q_vector = []
        global_ctxs_vector = []
        global_teacher_q_hidden = []
        global_teacher_ctx_hidden = []
        global_teacher_ctx_mask = []

        # ctxs_per_question = local_ctx_vectors.size(0)
        positive_idx_per_question = []
        # hard_negatives_per_question = []

        total_ctxs = 0

        for i, item in enumerate(global_question_ctx_vectors):
            q_vector, ctx_vectors, teacher_q_hidden, teacher_ctx_hidden, teacher_ctx_mask, positive_idx = item

            if i != args.local_rank:
                global_q_vector.append(q_vector.to(local_q_vector.device))
                global_ctxs_vector.append(ctx_vectors.to(local_q_vector.device))
                global_teacher_q_hidden.extend(teacher_q_hidden.to(local_q_vector.device))
                global_teacher_ctx_hidden.extend(teacher_ctx_hidden.to(local_q_vector.device))
                global_teacher_ctx_mask.extend(teacher_ctx_mask.to(local_q_vector.device))
                positive_idx_per_question.extend([v + total_ctxs for v in positive_idx])
            else:
                global_q_vector.append(local_q_vector)
                global_ctxs_vector.append(local_ctx_vectors)
                global_teacher_q_hidden.extend(local_teacher_q_hidden)
                global_teacher_ctx_hidden.extend(local_teacher_ctx_hidden)
                global_teacher_ctx_mask.extend(local_teacher_ctx_mask)
                positive_idx_per_question.extend(
                    [v + total_ctxs for v in local_positive_idxs]
                )
            total_ctxs += ctx_vectors.size(0)
        global_q_vector = torch.cat(global_q_vector, dim=0)
        global_ctxs_vector = torch.cat(global_ctxs_vector, dim=0)
        global_teacher_q_hidden = pad_sequence(global_teacher_q_hidden, batch_first=True)
        global_teacher_ctx_hidden = pad_sequence(global_teacher_ctx_hidden, batch_first=True)
        global_teacher_ctx_mask = pad_sequence(global_teacher_ctx_mask, batch_first=True)
        # global_teacher_q_hidden= torch.cat(global_teacher_q_hidden, dim=0)
        # global_teacher_ctx_hidden = torch.cat(global_teacher_ctx_hidden, dim=0)
    else:
        global_q_vector = local_q_vector
        global_ctxs_vector = local_ctx_vectors
        global_teacher_q_hidden = local_teacher_q_hidden
        global_teacher_ctx_hidden = local_teacher_ctx_hidden
        global_teacher_ctx_mask = local_teacher_ctx_mask
        positive_idx_per_question = local_positive_idxs

    loss_function = ColBERTKDLoss()
    loss, is_correct = loss_function.calc(
        args,
        global_q_vector,
        global_ctxs_vector,
        global_teacher_q_hidden,
        global_teacher_ctx_hidden,
        global_teacher_ctx_mask,
        positive_idx_per_question,
    )
    return loss, is_correct

def caculate_Col_NLLloss(args, local_q_hidden, local_ctx_hidden, local_positive_idxs):
    if torch.distributed.get_world_size() > 1:
        q_hidden_to_send = (
            torch.empty_like(local_q_hidden).cpu().copy_(local_q_hidden).detach_()
        )
        ctx_hidden_to_send = (
            torch.empty_like(local_ctx_hidden).cpu().copy_(local_ctx_hidden).detach_()
        )

        global_question_ctx_vectors = all_gather_list(
            [
                q_hidden_to_send,
                ctx_hidden_to_send,
                local_positive_idxs,
            ],
            max_size=640000000,
        )

        global_q_hidden = []
        global_ctx_hidden = []

        # ctxs_per_question = local_ctx_vectors.size(0)
        positive_idx_per_question = []
        # hard_negatives_per_question = []

        total_ctxs = 0

        for i, item in enumerate(global_question_ctx_vectors):
            q_hidden, ctx_hidden, positive_idx = item

            if i != args.local_rank:
                global_q_hidden.extend(q_hidden.to(local_q_hidden.device))
                global_ctx_hidden.extend(ctx_hidden.to(local_q_hidden.device))
                positive_idx_per_question.extend([v + total_ctxs for v in positive_idx])
            else:
                global_q_hidden.extend(local_q_hidden)
                global_ctx_hidden.extend(local_ctx_hidden)
                positive_idx_per_question.extend(
                    [v + total_ctxs for v in local_positive_idxs]
                )
            total_ctxs += ctx_hidden.size(0)

        global_q_hidden = pad_sequence(global_q_hidden, batch_first=True)
        global_ctx_hidden = pad_sequence(global_ctx_hidden, batch_first=True)
        #global_q_hidden = torch.cat(global_q_hidden, dim=0)
        #global_ctx_hidden = torch.cat(global_ctx_hidden, dim=0)
    else:
        global_q_hidden = local_q_hidden
        global_ctx_hidden = local_ctx_hidden
        positive_idx_per_question = local_positive_idxs

    loss_function = ColBERTNllLoss()
    loss, is_correct = loss_function.calc(
        args,
        global_q_hidden,
        global_ctx_hidden,
        positive_idx_per_question,
    )
    return loss, is_correct

def sum_main(x, opt):
    if opt.world_size > 1:
        dist.reduce(x, 0, op=dist.ReduceOp.SUM)
    return x
def evaluate_dev(args, model, tokenizer):
    dev_dataset = TraditionDataset(args.origin_data_dir_dev,tokenizer,num_hard_negatives = args.number_neg,is_training=False,
                                        max_seq_length=args.max_seq_length)
    dev_sample = RandomSampler(dev_dataset) if args.local_rank == -1 else DistributedSampler(dev_dataset)
    dev_dataloader = DataLoader(dev_dataset, sampler=dev_sample,
                        collate_fn=TraditionDataset.get_collate_fn(args),
                        batch_size=args.train_batch_size,num_workers=0,shuffle=False)
    correct_predictions_count_all = 0
    example_num = 0
    total_loss = 0
    model.eval()
    with torch.no_grad():
        for i, batch in enumerate(dev_dataloader):
            batch_retriever = batch['retriever']
            inputs_retriever = {"query_ids": batch_retriever[0].long().to(args.device),
                      "attention_mask_q": batch_retriever[1].long().to(args.device),
                      "input_ids_a": batch_retriever[2].long().to(args.device),
                      "attention_mask_a": batch_retriever[3].long().to(args.device)}

            if args.model_class == 'dual_encoder':
                local_q_vector, local_ctx_vectors = model(**inputs_retriever)
            elif args.model_class == 'ColBERT':
                local_q_vector, local_ctx_vectors, _, _ = model(**inputs_retriever)
            else:
                logger.info("no such type model" + args.model_class)
                exit(0)

            question_num = local_q_vector.size(0)
            retriever_local_ctx_vectors = local_ctx_vectors.reshape(question_num, local_ctx_vectors.size(0) // question_num, -1)

            rele_logits = torch.einsum("bh,bdh->bd", [local_q_vector, retriever_local_ctx_vectors])
            # retriever_dist_p = F.softmax(retriever_simila, dim=1)
            # batch_reranker = tuple(t.to(args.device) for t in batch['reranker'])
            # inputs_reranker = {"input_ids": batch_reranker[0].long(), "attention_mask": batch_reranker[1].long()}
            # output_reranker = model(**inputs_reranker)
            # binary_logits,relevance_logits,_ =output_reranker
            relevance_target = torch.zeros(rele_logits.size(0), dtype=torch.long).to(args.device)
            loss_fct = torch.nn.CrossEntropyLoss()
            relative_loss = loss_fct(rele_logits,relevance_target)
            total_loss+=relative_loss
            max_score, max_idxs = torch.max(rele_logits, 1)
            correct_predictions_count = (max_idxs == 0).sum()
            correct_predictions_count_all+=correct_predictions_count
            example_num += batch['reranker'][1].size(0)
    example_num = torch.tensor(1).to(rele_logits)*example_num
    total_loss = torch.tensor(1).to(rele_logits)*total_loss
    correct_predictions_count_all = torch.tensor(1).to(rele_logits)*correct_predictions_count_all
    correct_predictions_count_all = sum_main(correct_predictions_count_all,args)
    example_num = sum_main(example_num,args)
    total_loss = sum_main(total_loss,args)
    total_loss = total_loss / i
    correct_ratio = float(correct_predictions_count_all / example_num)
    logger.info('NLL Validation: loss = %f. correct prediction ratio  %d/%d ~  %f', total_loss,
                correct_predictions_count_all.item(),
                example_num.item(),
                correct_ratio
                )

    model.train()
    return total_loss, correct_ratio


def do_biencoder_fwd_pass_eval(args, model, batch):
    batch = tuple(t.to(args.device) for t in batch)
    inputs = {"query_ids": batch[0][::2].long(), "attention_mask_q": batch[1][::2].long(),
              "input_ids_a": batch[3].long(), "attention_mask_a": batch[4].long()}

    local_q_vector, local_ctx_vectors = model(**inputs)

    q_vector_to_send = torch.empty_like(local_q_vector).cpu().copy_(local_q_vector).detach_()
    ctx_vector_to_send = torch.empty_like(local_ctx_vectors).cpu().copy_(local_ctx_vectors).detach_()

    global_question_ctx_vectors = all_gather_list(
        [q_vector_to_send, ctx_vector_to_send],
        max_size=640000000)

    global_q_vector = []
    global_ctxs_vector = []

    for i, item in enumerate(global_question_ctx_vectors):
        q_vector, ctx_vectors = item

        if i != args.rank:
            global_q_vector.append(q_vector.to(local_q_vector.device))
            global_ctxs_vector.append(ctx_vectors.to(local_q_vector.device))
        else:
            global_q_vector.append(local_q_vector)
            global_ctxs_vector.append(local_ctx_vectors)

    global_q_vector = torch.cat(global_q_vector, dim=0)
    global_ctxs_vector = torch.cat(global_ctxs_vector, dim=0)

    scores = torch.matmul(global_q_vector, torch.transpose(global_ctxs_vector, 0, 1))
    if len(global_q_vector.size()) > 1:
        q_num = global_q_vector.size(0)
        scores = scores.view(q_num, -1)
    softmax_scores = F.log_softmax(scores, dim=1)
    positive_idx_per_question = [i * 2 for i in range(q_num)]
    loss = F.nll_loss(softmax_scores, torch.tensor(positive_idx_per_question).to(softmax_scores.device),
                      reduction='mean')
    max_score, max_idxs = torch.max(softmax_scores, 1)
    correct_predictions_count = (max_idxs == torch.tensor(positive_idx_per_question).to(max_idxs.device)).sum()

    is_correct = correct_predictions_count.sum().item()

    if args.n_gpu > 1:
        loss = loss.mean()
    if args.gradient_accumulation_steps > 1:
        loss = loss / args.gradient_accumulation_steps

    return loss, is_correct

def triplet_fwd_pass(args, model, batch):
    batch = tuple(t.to(args.device) for t in batch)
    inputs = {"query_ids": batch[0].long(), "attention_mask_q": batch[1].long(),
              "input_ids_a": batch[3].long(), "attention_mask_a": batch[4].long(),
              "input_ids_b": batch[6].long(), "attention_mask_b": batch[7].long()}
    loss = model(**inputs)[0]

    if args.n_gpu > 1:
        loss = loss.mean()
    if args.gradient_accumulation_steps > 1:
        loss = loss / args.gradient_accumulation_steps

    return loss


def _save_checkpoint(args, model, optimizer, scheduler, step: int) -> str:
    offset = step
    epoch = 0
    model_to_save = get_model_obj(model)
    cp = os.path.join(args.output_dir, 'checkpoint-' + str(offset))

    meta_params = {}

    state = CheckpointState(model_to_save.state_dict(),
                            optimizer.state_dict(),
                            scheduler.state_dict(),
                            offset,
                            epoch, meta_params
                            )
    torch.save(state._asdict(), cp)
    logger.info('Saved checkpoint at %s', cp)
    return cp


def _load_saved_state(model, optimizer, scheduler, saved_state: CheckpointState):
    epoch = saved_state.epoch
    step = saved_state.offset
    logger.info('Loading checkpoint @ step=%s', step)

    model_to_load = get_model_obj(model)
    logger.info('Loading saved model state ...')
    model_to_load.load_state_dict(saved_state.model_dict)  # set strict=False if you use extra projection
    optimizer.load_state_dict(saved_state.optimizer_dict)
    scheduler.load_state_dict(saved_state.scheduler_dict)
    return step


def get_arguments():
    parser = argparse.ArgumentParser()

    # Required parameters
    parser.add_argument(
        "--model_type",
        default=None,
        type=str,
        required=True,
        help="Model type selected in the list:",
    )
    parser.add_argument(
        "--teacher_model_type",
        default=None,
        type=str,
        required=True,
        help="Teacher Model type selected in the list:",
    )
    parser.add_argument(
        "--model_name_or_path",
        default=None,
        type=str,
    )
    parser.add_argument(
        "--model_name_or_path_ict",
        default=None,
        type=str,
    )
    parser.add_argument(
        "--teacher_type",
        default="dual_encoder",
        type=str,
    )
    parser.add_argument(
        "--model_class",
        default="dual_encoder",
        type=str,
    )
    parser.add_argument(
        "--teacher_model_path",
        default=None,
        type=str,
    )
    parser.add_argument(
        "--student_model_path",
        default=None,
        type=str,
    )
    parser.add_argument(
        "--ts_share_weight",
        default=False,
        type=bool,
    )


    parser.add_argument(
        "--output_dir",
        default=None,
        type=str,
        required=True,
        help="The output directory where the model predictions and checkpoints will be written.",
    )

    parser.add_argument(
        "--num_epoch",
        default=0,
        type=int,
        help="Number of epoch to train, if specified will use training data instead of ann",
    )

    # Other parameters
    parser.add_argument(
        "--config_name", default="", type=str, help="Pretrained config name or path if not the same as model_name",
    )
    parser.add_argument(
        "--tokenizer_name",
        default="",
        type=str,
        help="Pretrained tokenizer name or path if not the same as model_name",
    )
    parser.add_argument(
        "--cache_dir",
        default="",
        type=str,
        help="Where do you want to store the pre-trained models downloaded from s3",
    )
    parser.add_argument(
        "--max_seq_length",
        default=128,
        type=int,
        help="The maximum total input sequence length after tokenization. Sequences longer "
             "than this will be truncated, sequences shorter will be padded.",
    )

    parser.add_argument(
        "--max_query_length",
        default=64,
        type=int,
        help="The maximum total input sequence length after tokenization. Sequences longer "
             "than this will be truncated, sequences shorter will be padded.",
    )

    parser.add_argument(
        "--student_num_hidden_layers",
        default=6,
        type=int,
        help="Layers of student model",
    )

    parser.add_argument(
        "--teacher_num_hidden_layers",
        default=12,
        type=int,
        help="Layers of teacher model",
    )

    parser.add_argument("--triplet", default=False, action="store_true", help="Whether to run training.")
    parser.add_argument(
        "--log_dir",
        default=None,
        type=str,
        help="Tensorboard log dir",
    )

    parser.add_argument(
        "--optimizer",
        default="adamW",
        type=str,
        help="Optimizer - lamb or adamW",
    )

    parser.add_argument(
        "--per_gpu_train_batch_size", default=8, type=int, help="Batch size per GPU/CPU for training.",
    )
    parser.add_argument(
        "--gradient_accumulation_steps",
        type=int,
        default=1,
        help="Number of updates steps to accumulate before performing a backward/update pass.",
    )
    parser.add_argument("--learning_rate", default=5e-5, type=float, help="The initial learning rate for Adam.")
    parser.add_argument("--weight_decay", default=0.0, type=float, help="Weight decay if we apply some.")
    parser.add_argument("--adam_epsilon", default=1e-8, type=float, help="Epsilon for Adam optimizer.")
    parser.add_argument("--max_grad_norm", default=2.0, type=float, help="Max gradient norm.")
    parser.add_argument(
        "--max_steps",
        default=300000,
        type=int,
        help="If > 0: set total number of training steps to perform",
    )
    parser.add_argument("--warmup_steps", default=0, type=int, help="Linear warmup over warmup_steps.")
    parser.add_argument("--logging_steps", type=int, default=500, help="Log every X updates steps.")
    parser.add_argument("--save_steps", type=int, default=500, help="Save checkpoint every X updates steps.")

    parser.add_argument("--no_cuda", action="store_true", help="Avoid using CUDA when available")
    parser.add_argument("--seed", type=int, default=42, help="random seed for initialization")
    parser.add_argument("--off_inbatch", default=False, action='store_true', help="whether to use in-batch neg")
    parser.add_argument("--open_LwF", default=False, action='store_true', help="whether to use Stable Distillation")

    parser.add_argument(
        "--fp16",
        action="store_true",
        help="Whether to use 16-bit (mixed) precision (through NVIDIA apex) instead of 32-bit",
    )
    parser.add_argument(
        "--fp16_opt_level",
        type=str,
        default="O1",
        help="For fp16: Apex AMP optimization level selected in ['O0', 'O1', 'O2', and 'O3']."
             "See details at https://nvidia.github.io/apex/amp.html",
    )
    parser.add_argument(
            "--gradient_checkpointing",
            default=False,
            action="store_true",
        )
    parser.add_argument(
            "--origin_data_dir",
            default=None,
            type=str,
        )
    parser.add_argument(
            "--origin_data_dir_dev",
            default=None,
            type=str,
        )
    # ----------------- ANN HyperParam ------------------

    parser.add_argument(
        "--load_optimizer_scheduler",
        default=False,
        action="store_true",
        help="load scheduler from checkpoint or not",
    )

    parser.add_argument(
        "--single_warmup",
        default=True,
        action="store_true",
        help="use single or re-warmup",
    )

    parser.add_argument("--adv_data_path",
                        type=str,
                        default=None,
                        help="adv_data_path", )
                        
    parser.add_argument("--ann_data_path",
                        type=str,
                        default=None,
                        help="adv_data_path", )
    parser.add_argument(
            "--fix_embedding",
            default=False,
            action="store_true",
            help="use single or re-warmup",
        )
    parser.add_argument(
            "--continue_train",
            default=False,
            action="store_true",
            help="use single or re-warmup",
        )
    parser.add_argument(
            "--shuffle_positives",
            default=False,
            action="store_true",
            help="use single or re-warmup")
    parser.add_argument("--reranker_model_path", type=str, default="", help="For distant debugging.")
    parser.add_argument("--reranker_model_type", type=str, default="", help="For distant debugging.")
    parser.add_argument("--number_neg", type=int, default=20, help="hard neg num")
    parser.add_argument("--neg_type", type=str, default="random", help="neg type")
    parser.add_argument("--number_easy_neg", type=int, default=0, help="easy neg num")
    parser.add_argument("--adv_max_norm", default=0., type=float)
    parser.add_argument("--adv_init_mag", default=0, type=float)
    parser.add_argument("--adv_lr", default=5e-2, type=float)
    parser.add_argument("--adv_steps", default=3, type=int)
    parser.add_argument("--scale_simmila", default=False, action="store_true")
    # ----------------- End of Doc Ranking HyperParam ------------------
    parser.add_argument("--local_rank", type=int, default=-1, help="For distributed training: local_rank")
    parser.add_argument("--server_ip", type=str, default="", help="For distant debugging.")
    parser.add_argument("--server_port", type=str, default="", help="For distant debugging.")

    #------------------KD setting-----------------------------
    parser.add_argument("--KD_type", type=str, default=None, help="Type of Distillation")
    parser.add_argument("--CE_WEIGHT", type=float, default=0.5, help="Classification loss proportion")
    parser.add_argument("--KD_WEIGHT", type=float, default=0.5, help="Distillation loss proportion")
    parser.add_argument("--LwF_WEIGHT", type=float, default=None, help="LwF loss proportion")
    parser.add_argument("--TEMPERATURE", type=float, default=4.0, help="Distillation temperature")

    parser.add_argument("--DKD_alpha", type=float, default=None, help="DKD Distillation alpha")
    parser.add_argument("--DKD_beta", type=float, default=None, help="DKD Distillation beta")

    #------------------ColBERT setting--------------------------
    parser.add_argument("--similarity_metric", type=str, default='cosine', help="ColBERT score computing method")

    #------------------teacher step setting----------------------
    parser.add_argument("--teacher_step", type=bool, default=False, help="Teacher model parameter update on/off")
    parser.add_argument("--teacher_learning_rate", default=5e-5, type=float, help="The teacher model initial learning rate for Adam.")

    #-----------------double teacher setting-----------------------
    parser.add_argument("--double_teacher", type=str, default=None, help="double teacher training")
    parser.add_argument("--double_teacher_pretrain", type=str, default=None, help="double teacher training")
    parser.add_argument("--double_teacher_type", type=str, default=None, help="double teacher training")
    parser.add_argument("--double_teacher_num_hidden_layers", type=int, default=12, help="double teacher training")

    args = parser.parse_args()
    return args


def set_env(args):
    # Setup distant debugging if needed
    if args.server_ip and args.server_port:
        # Distant debugging - see https://code.visualstudio.com/docs/python/debugging#_attach-to-a-local-script
        import ptvsd

        print("Waiting for debugger attach")
        ptvsd.enable_attach(address=(args.server_ip, args.server_port), redirect_output=True)
        ptvsd.wait_for_attach()

    # Setup CUDA, GPU & distributed training
    if args.local_rank == -1 or args.no_cuda:
        device = torch.device("cuda" if torch.cuda.is_available() and not args.no_cuda else "cpu")
        args.n_gpu = torch.cuda.device_count()
    else:  # Initializes the distributed backend which will take care of sychronizing nodes/GPUs
        torch.cuda.set_device(args.local_rank)
        device = torch.device("cuda", args.local_rank)
        torch.distributed.init_process_group(backend="nccl")
        args.n_gpu = 1
    args.device = device

    # Setup logging
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s -   %(message)s",
        datefmt="%m/%d/%Y %H:%M:%S",
        level=logging.INFO if args.local_rank in [-1, 0] else logging.WARN,
    )
    logger.warning(
        "Process rank: %s, device: %s, n_gpu: %s, distributed training: %s, 16-bits training: %s",
        args.local_rank,
        device,
        args.n_gpu,
        bool(args.local_rank != -1),
        args.fp16,
    )

    # Set seed
    set_seed(args)


def load_states_from_checkpoint_ict(model_file: str) -> CheckpointState:
    from torch.serialization import default_restore_location
    logger.info('Reading saved model from %s', model_file)
    state_dict = torch.load(model_file, map_location=lambda s, l: default_restore_location(s, 'cpu'))
    logger.info('model_state_dict keys %s', state_dict.keys())
    new_stae_dict = {}
    for key,value in state_dict['model']['query_model']['language_model'].items():
        new_stae_dict['question_model.'+key] = value
    for key,value in state_dict['model']['context_model']['language_model'].items():
        new_stae_dict['ctx_model.'+key] = value
    return new_stae_dict

def load_model(args):
    # Prepare GLUE task
    args.output_mode = "classification"
    label_list = ["0", "1"]
    num_labels = len(label_list)

    # store args
    if args.local_rank != -1:
        args.world_size = torch.distributed.get_world_size()
        args.rank = dist.get_rank()

    # Load pretrained model and tokenizer
    if args.local_rank not in [-1, 0]:
        torch.distributed.barrier()  # Make sure only the first process in distributed training will download model & vocab

    args.model_type = args.model_type
   
    if is_first_worker():
        # Create output directory if needed
        if not os.path.exists(args.output_dir):
            os.makedirs(args.output_dir)
    # tokenizer = BertTokenizer.from_pretrained(
    #         "bert-base-uncased",
    #         do_lower_case=True)
    tokenizer_path = "bert-base-uncased"
    tokenizer = BertTokenizer.from_pretrained(tokenizer_path, do_lower_case=True)
    model = BiBertEncoder(args, role='student')
    if args.model_name_or_path_ict is not None:
        saved_state = load_states_from_checkpoint_ict(args.model_name_or_path_ict)
        model.load_state_dict(saved_state)

    # load student model
    if args.student_model_path is not None:
        saved_state = load_states_from_checkpoint(args.student_model_path)
        model.load_state_dict(saved_state.model_dict)
        logger.info("load student model at " + args.student_model_path)

    # # 自动读取last checkpoint
    # if args.output_dir is not None:
    #     checkpoint_files = []
    #     if os.path.exists(args.output_dir):
    #         for item in os.scandir(args.output_dir):
    #             if item.is_file():
    #                 if "checkpoint" in item.path:
    #                     checkpoint_files.append(item.path)
    #         if len(checkpoint_files) != 0:
    #             checkpoint_files.sort(key=lambda f: int(f.split('checkpoint-')[1]),reverse=True)
    #             logger.info("***** load " + checkpoint_files[0] + " *****")
    #             saved_state = load_states_from_checkpoint(checkpoint_files[0])
    #             model.load_state_dict(saved_state)
    #         else:
    #             logger.info("***** there are no checkpoint in" + args.output_dir + " *****")

        
    #global_step = _load_saved_state(model, optimizer, scheduler, saved_state)
    if args.fix_embedding:
        word_embedding = model.ctx_model.get_input_embeddings()
        word_embedding.requires_grad = False

    if args.local_rank == 0:
        torch.distributed.barrier()  # Make sure only the first process in distributed training will download model & vocab

    model.to(args.device)
    return tokenizer, model

def load_teacher_model(args):

    if args.teacher_type == "dual_encoder":
        model = BiBertEncoder(args, role='teacher')
    elif args.teacher_type == "ColBERT":
        model = ColBERT(args, role='teacher')
    elif args.teacher_type == "cross_encoder":
        encoder = HFBertEncoder.init_encoder(args, role='teacher')
        hidden_size = encoder.config.hidden_size
        model = Reranker(encoder, hidden_size)
        pass
    else:
        logger.info("no such type of teacher model " + args.teacher_type)
        exit(0)

    if args.teacher_model_path is not None:
        saved_state = load_states_from_checkpoint(args.teacher_model_path)
        model.load_state_dict(saved_state.model_dict)
        logger.info("load teacher model at "+args.teacher_model_path)
    else:
        logger.info("use teacher model initial parameters to training.")
    # else:
    #     logger.info("KD train must have teacher model,plese check if there is teacher model under" + args.teacher_model_path)
    #     exit(0)
    model.to(args.device)
    return model

def load_double_teacher_model(args):

    if args.double_teacher_type == "dual_encoder":
        model = BiBertEncoder(args, role='double_teacher')
    elif args.double_teacher_type == "ColBERT":
        model = ColBERT(args, role='double_teacher')
    elif args.double_teacher_type == "cross_encoder":
        encoder = HFBertEncoder.init_encoder(args, role='double_teacher')
        hidden_size = encoder.config.hidden_size
        model = Reranker(encoder, hidden_size)
        pass
    else:
        logger.info("no such type of double teacher model " + args.double_teacher_type)
        exit(0)

    if args.double_teacher is not None:
        saved_state = load_states_from_checkpoint(args.double_teacher)
        model.load_state_dict(saved_state.model_dict)
        logger.info("load teacher model at "+args.double_teacher)
    else:
        logger.info("use teacher model initial parameters to training.")
    # else:
    #     logger.info("KD train must have teacher model,plese check if there is teacher model under" + args.teacher_model_path)
    #     exit(0)
    model.to(args.device)
    return model


def main():
    args = get_arguments()
    set_env(args)
    logger.info("training using KD tpye : " + args.KD_type)
    tokenizer, model = load_model(args)
    teacher_model = load_teacher_model(args)

    if args.double_teacher != None:
        double_teacher = load_double_teacher_model(args)
    else:
        double_teacher = None

    basic_format = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    formatter = logging.Formatter(basic_format)
    log_path = os.path.join(args.output_dir, 'log.txt')
    # sh = logging.StreamHandler()
    handler = logging.FileHandler(log_path, 'a', 'utf-8')

    handler.setFormatter(formatter)
    logger.addHandler(handler)
    # logger.addHandler(sh)
    logger.setLevel(logging.INFO if args.local_rank in [-1, 0] else logging.WARN)
    print(logger)

    if args.ts_share_weight:
        if args.teacher_num_hidden_layers == args.student_num_hidden_layers:
            model = teacher_model
        else:
            logger.info("error : The student model and the teacher model must be set at the same num_hidden_layers.")
            exit(0)

    global_step = train(args, double_teacher, teacher_model, model, tokenizer)
    logger.info(" global_step = %s", global_step)

    if args.local_rank != -1:
        dist.barrier()


if __name__ == "__main__":
    main()
