"""Trainer module for training seq2seq model
"""

import json
import os
import pickle
from datetime import datetime
from tqdm import tqdm

import numpy as np
import torch
import torch.nn as nn
from torch import optim
from torch.autograd import Variable

import constants as C
from language_models.model import LM

USE_CUDA = torch.cuda.is_available()

class Trainer:

    def __init__(self, 
        dataloader, params,
        random_seed=1, 
        save_model_every=1,     # Every Number of epochs to save after
        print_every=100,        # Every Number of batches to print after
        dev_loader=None,
        vocab=None
    ):
        _set_random_seeds(random_seed)

        self.save_model_every = save_model_every
        self.print_every = print_every
        self.params = params
        self.dataloader = dataloader
        self.vocab = vocab
        self.model = LM(
            params[C.VOCAB_SIZE],
            params[C.HDIM],
            params[C.OUTPUT_MAX_LEN],
            params[C.H_LAYERS],
            params[C.DROPOUT]
        )

        if USE_CUDA: self.model = self.model.cuda()

        self.loss = []
        self.perplexity = []
        self.criterion = nn.NLLLoss()

        self.model = None
        self.optimizer = None
        self.params = None

        self.vocabs = None

    def train_batch(self, 
            quesion_seqs,
            review_seqs,
            answer_seqs,
            answer_lengths
        ):
        self.optimizer.zero_grad()

        answer_seqs = _var(answer_seqs)
        quesion_seqs = None if self.model == C.LM_ANSWERS else _var(quesion_seqs)
        review_seqs = map(_var, review_seqs) if self.model == C.LM_QUESTION_ANSWERS_REVIEWS else None
        target_seqs = _var(answer_seqs)

        # run forward pass
        teacher_forcing = np.random.random() < self.params[C.TEACHER_FORCING_RATIO]
        outputs, _, _ = self.model(
            quesion_seqs,
            review_seqs,
            answer_seqs,
            teacher_forcing
        )

        # loss and gradient computation
        loss = _batch_loss(self.criterion, outputs, answer_lengths, target_seqs)
        loss.backward()

        # update parameters
        self.optimizer.step()

        return loss.data[0]

    def train(self):
        self._set_optimizer()

        for epoch in tqdm(range(self.params[C.EPOCHS])):
            print('Epoch: %d', epoch)
            for batch_itr, inputs in tqdm(enumerate(self.dataloader)):
                if self.model == C.LM_ANSWERS:
                    answer_seqs, answer_lengths = inputs
                elif self.model == C.LM_QUESTION_ANSWERS:
                    (answer_seqs, answer_lengths), quesion_seqs = inputs
                elif self.model == C.LM_QUESTION_ANSWERS:
                    (answer_seqs, answer_lengths), quesion_seqs, review_seqs = inputs
                else:
                    raise 'Unimplemented model: %s' % self.model
                loss = self.train_batch(
                    quesion_seqs,
                    review_seqs,
                    answer_seqs,
                    answer_lengths
                )
                self.loss.append(loss)
                self.perplexity.append(_perplexity_from_loss(loss))
                if batch_itr % self.print_every == 0:
                    print('Loss at batch %d = %.2f', (batch_itr, self.loss[-1]))
                    print('Perplexity at batch %d = %.2f', (batch_itr, self.perplexity[-1]))
            if epoch % self.save_model_every == 0:
                self.save_model()
            if epoch == self.params[C.DECAY_START_EPOCH]:
                self.optimizer = self._set_optimizer(lr_decay=self.params[C.LR_DECAY])

    def eval(self):
        dev_losses, dev_perplexities = [], []
        for batch_itr, inputs in tqdm(enumerate(self.dataloader)):
            if self.model == C.LM_ANSWERS:
                answer_seqs, answer_lengths = inputs
            elif self.model == C.LM_QUESTION_ANSWERS:
                (answer_seqs, answer_lengths), quesion_seqs = inputs
            elif self.model == C.LM_QUESTION_ANSWERS:
                (answer_seqs, answer_lengths), quesion_seqs, review_seqs = inputs
            else:
                raise 'Unimplemented model: %s' % self.model

            answer_seqs = _var(answer_seqs)
            quesion_seqs = None if self.model == C.LM_ANSWERS else _var(quesion_seqs)
            review_seqs = map(_var, review_seqs) if self.model == C.LM_QUESTION_ANSWERS_REVIEWS else None
            target_seqs = _var(answer_seqs)
            outputs, _, _ = self.model(
                quesion_seqs,
                review_seqs,
                answer_seqs,
                False
            )

            dev_loss = _batch_loss(self.criterion, outputs, answer_lengths, target_seqs)
            dev_losses.append(dev_loss.data[0])
            dev_perplexities.append(_perplexity_from_loss(dev_loss.data[0]))
            if batch_itr % self.print_every == 0:
                print('[Dev] Loss at batch %d = %.2f', (batch_itr, dev_loss[-1]))
                print('[Dev] Perplexity at batch %d = %.2f', (batch_itr, dev_perplexities[-1]))
        _print_info(1, dev_losses, dev_perplexities, 'Development')
        return

    def save_model(self):
        save_dir = self._save_dir(datetime.now())
        _ensure_path(save_dir)

        model_filename = '%s/%s' % (save_dir, C.SAVED_MODEL_FILENAME)
        params_filename = '%s/%s' % (save_dir, C.SAVED_PARAMS_FILENAME)
        vocab_filename = '%s/%s' & (save_dir, C.SAVED_VOCAB_FILENAME)

        torch.save(self.model.state_dict(), model_filename)
        with open(params_filename, 'w') as fp:
            json.dump(self.params, fp, indent=4, sort_keys=True)
        
        with open(vocab_filename, 'wb') as fp:
            pickle.dump(self.vocab, fp, pickle.HIGHEST_PROTOCOL)

    def _save_dir(self, time):
        time_str = time.strftime('%Y-%m-%d-%H-%M-%S')
        return '%s/%s/%s' % (C.BASE_PATH, self.params[C.MODEL_NAME], time_str)

    def _set_optimizer(self, lr_decay=1.0):
        self.optimizer = optim.SGD(self.model.parameters(), lr=self.params[C.LR] * lr_decay)

def _set_random_seeds(seed):
    np.random.seed(seed)
    torch.manual_seed(seed)

def _batch_loss(criterion, outputs, target_lengths, targets):
    loss = 0
    l = np.array(target_lengths)
    for idx in range(len(target_lengths)):
        idxes = l > 0
        loss += criterion(outputs[idx][idxes], targets[idxes, idx])
        l -= 1
    return loss / len(outputs)

def _ensure_path(path):
    if not os.path.exists(path):
        os.makedirs(path)

def _perplexity_from_loss(loss):
    return np.power(2.0, loss)

def _print_info(epoch, losses, perplexities, corpus):
    print('Epoch = %d, [%s] Loss = %.2f', (epoch, corpus, np.mean(np.array(losses))))
    print('Epoch = %d, [%s] Perplexity = %.2f', (epoch, corpus, np.mean(np.array(perplexities))))

def _var(variable):
    dtype = torch.cuda.FloatTensor if USE_CUDA else torch.FloatTensor
    return Variable(torch.LongTensor(variable).type(dtype))