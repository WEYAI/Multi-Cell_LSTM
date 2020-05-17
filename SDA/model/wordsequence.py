# -*- coding: utf-8 -*-

from __future__ import print_function
from __future__ import absolute_import
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.nn.utils.rnn import pack_padded_sequence
from .wordrep import WordRep
from .MultiCell_LSTM_compose import MultiCellLSTM
from .LSTM_base import LSTM
from .cell2entity import Cell_to_Entity

class WordSequence(nn.Module):
    def __init__(self, data):
        super(WordSequence, self).__init__()
        print("build word sequence feature extractor: %s..."%(data.word_feature_extractor))
        self.gpu = data.HP_gpu
        self.use_char = data.use_char
        self.droplstm = nn.Dropout(data.HP_dropout)
        self.bilstm_flag = data.HP_bilstm
        self.lstm_layer = data.HP_lstm_layer
        self.wordrep = WordRep(data)
        self.input_size = data.word_emb_dim
        if data.pretrain == 'ELMo':
            self.input_size = 1024 + self.input_size
        if data.pretrain == 'BERT': # bert
            self.input_size = 768 + self.input_size # bert-base
        self.feature_num = data.feature_num
        if self.use_char:
            self.input_size += data.HP_char_hidden_dim
            if data.char_feature_extractor == "ALL":
                self.input_size += data.HP_char_hidden_dim
        for idx in range(self.feature_num):
            self.input_size += data.feature_emb_dims[idx]
        # The LSTM takes word embeddings as inputs, and outputs hidden states
        # with dimensionality hidden_dim.
        if self.bilstm_flag:
            lstm_hidden = data.HP_hidden_dim // 2
        else:
            lstm_hidden = data.HP_hidden_dim
        if data.task == 'NER' or data.task == 'Chunk':
            self.entity_number = len(data.entity_type)
        elif data.task == 'POSTag':
            self.entity_number = len(data.POS_type)
        self.entity_mask_S = data.entity_mask_S
        self.entity_mask_T = data.entity_mask_T
        self.word_feature_extractor = data.word_feature_extractor
        if self.word_feature_extractor == "LSTM":
            self.lstm = LSTM(self.input_size, lstm_hidden, bidirectional=self.bilstm_flag, dropout=data.HP_dropout)
        elif self.word_feature_extractor == "MultiCellLSTM":
            self.lstm = MultiCellLSTM(self.input_size, lstm_hidden, self.entity_number, self.entity_mask_S, self.entity_mask_T, left2right=True, use_bias=True, gpu=self.gpu)
            if self.bilstm_flag:
                self.lstm_back = MultiCellLSTM(self.input_size, lstm_hidden, self.entity_number, self.entity_mask_S, self.entity_mask_T, left2right=False, use_bias=True, gpu=self.gpu)
        elif self.word_feature_extractor == "CNN":
            # cnn_hidden = data.HP_hidden_dim
            self.word2cnn = nn.Linear(self.input_size, data.HP_hidden_dim)
            self.cnn_layer = data.HP_cnn_layer
            print("CNN layer: ", self.cnn_layer)
            self.cnn_list = nn.ModuleList()
            self.cnn_drop_list = nn.ModuleList()
            self.cnn_batchnorm_list = nn.ModuleList()
            kernel = 3
            pad_size = int((kernel-1)/2)
            for idx in range(self.cnn_layer):
                self.cnn_list.append(nn.Conv1d(data.HP_hidden_dim, data.HP_hidden_dim, kernel_size=kernel, padding=pad_size))
                self.cnn_drop_list.append(nn.Dropout(data.HP_dropout))
                self.cnn_batchnorm_list.append(nn.BatchNorm1d(data.HP_hidden_dim))
        # The linear layer that maps from hidden state space to tag space
        self.hidden2tag_S = nn.Linear(data.HP_hidden_dim, data.label_alphabet_size_S)
        self.hidden2tag_T = nn.Linear(data.HP_hidden_dim, data.label_alphabet_size_T)
        self.cell2entity = Cell_to_Entity(data.HP_hidden_dim, self.entity_number, self.entity_mask_S, self.entity_mask_T)


    def forward(self, original_words_batch, domain_tag, word_inputs, feature_inputs, word_seq_lengths, char_inputs, char_seq_lengths, char_seq_recover, mask, batch_entity=None):
        """
            input:
                word_inputs: (batch_size, sent_len)
                feature_inputs: [(batch_size, sent_len), ...] list of variables
                word_seq_lengths: list of batch_size, (batch_size,1)
                char_inputs: (batch_size*sent_len, word_length)
                char_seq_lengths: list of whole batch_size for char, (batch_size*sent_len, 1)
                char_seq_recover: variable which records the char order information, used to recover char order
            output:
                Variable(batch_size, sent_len, hidden_dim)
        """
        
        word_represent = self.wordrep(original_words_batch, word_inputs, feature_inputs, word_seq_lengths, char_inputs, char_seq_lengths, char_seq_recover, mask)
        ## word_embs (batch_size, seq_len, embed_size)
        if self.word_feature_extractor == "CNN":
            batch_size = word_inputs.size(0)
            word_in = torch.tanh(self.word2cnn(word_represent)).transpose(2,1).contiguous()
            for idx in range(self.cnn_layer):
                if idx == 0:
                    cnn_feature = F.relu(self.cnn_list[idx](word_in))
                else:
                    cnn_feature = F.relu(self.cnn_list[idx](cnn_feature))
                cnn_feature = self.cnn_drop_list[idx](cnn_feature)
                if batch_size > 1:
                    cnn_feature = self.cnn_batchnorm_list[idx](cnn_feature)
            feature_out = cnn_feature.transpose(2,1).contiguous()
            outputs = self.hidden2tag(feature_out)
        elif self.word_feature_extractor == "LSTM":  # lstm
            hidden = None
            lstm_out = self.lstm(word_represent, mask, hidden)
            hidden_outputs = self.droplstm(lstm_out)
            if domain_tag == "Source":
                outputs = self.hidden2tag_S(hidden_outputs)
            elif domain_tag == "Target":
                outputs = self.hidden2tag_T(hidden_outputs)
            cell_out_all, atten_scores_all = None, None

        elif self.word_feature_extractor == "MultiCellLSTM": # MultiCellLSTM
            hidden = None
            hidden_outputs, cell_states, atten_scores = self.lstm(domain_tag, word_represent, mask, hidden)
            if self.bilstm_flag:
                back_hidden = None
                hidden_outputs_back, cell_states_back, atten_scores_back = self.lstm_back(domain_tag, word_represent, mask, back_hidden)
                hidden_outputs = torch.cat([hidden_outputs, hidden_outputs_back], dim=-1)
                cell_states_all = torch.cat([cell_states, cell_states_back], dim=-1)
                atten_scores_all = (atten_scores + atten_scores_back) * 0.5

            hidden_outputs = self.droplstm(hidden_outputs)
            cell_states_all = self.droplstm(cell_states_all)
            if domain_tag == "Source":
                outputs = self.hidden2tag_S(hidden_outputs)
            elif domain_tag == "Target":
                outputs = self.hidden2tag_T(hidden_outputs)
            cell_out_all = self.cell2entity(domain_tag, cell_states_all)

        return outputs, cell_out_all, atten_scores_all

    def sentence_representation(self, word_inputs, feature_inputs, word_seq_lengths, char_inputs, char_seq_lengths, char_seq_recover):
        """
            input:
                word_inputs: (batch_size, sent_len)
                feature_inputs: [(batch_size, ), ...] list of variables
                word_seq_lengths: list of batch_size, (batch_size,1)
                char_inputs: (batch_size*sent_len, word_length)
                char_seq_lengths: list of whole batch_size for char, (batch_size*sent_len, 1)
                char_seq_recover: variable which records the char order information, used to recover char order
            output:
                Variable(batch_size, sent_len, hidden_dim)
        """

        word_represent = self.wordrep(word_inputs, feature_inputs, word_seq_lengths, char_inputs, char_seq_lengths, char_seq_recover)
        ## word_embs (batch_size, seq_len, embed_size)
        batch_size = word_inputs.size(0)
        if self.word_feature_extractor == "CNN":
            word_in = torch.tanh(self.word2cnn(word_represent)).transpose(2,1).contiguous()
            for idx in range(self.cnn_layer):
                if idx == 0:
                    cnn_feature = F.relu(self.cnn_list[idx](word_in))
                else:
                    cnn_feature = F.relu(self.cnn_list[idx](cnn_feature))
                cnn_feature = self.cnn_drop_list[idx](cnn_feature)
                if batch_size > 1:
                    cnn_feature = self.cnn_batchnorm_list[idx](cnn_feature)
            feature_out = F.max_pool1d(cnn_feature, cnn_feature.size(2)).view(batch_size, -1)
        else:
            packed_words = pack_padded_sequence(word_represent, word_seq_lengths.cpu().numpy(), True)
            hidden = None
            lstm_out, hidden = self.lstm(packed_words, hidden)

            feature_out = hidden[0].transpose(1, 0).contiguous().view(batch_size, -1)
            
        feature_list = [feature_out]
        for idx in range(self.feature_num):
            feature_list.append(self.feature_embeddings[idx](feature_inputs[idx]))
        final_feature = torch.cat(feature_list, 1)
        outputs = self.hidden2tag(self.droplstm(final_feature))
        ## outputs: (batch_size, label_alphabet_size)
        return outputs
