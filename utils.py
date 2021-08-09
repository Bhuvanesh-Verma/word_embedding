from typing import Any

import nltk
import numpy as np
import os, string
from collections import Counter, defaultdict as dd
import re, torch
import torch.nn as nn
import random
# from scipy.spatial.distance import cosine as cosine_similarity
from sklearn.metrics.pairwise import cosine_similarity
from torch import Tensor
from torch.utils.data import Dataset
import yaml

with open('./config.yml', 'r') as f:
    config = yaml.safe_load(f)

device = 'cuda' if torch.cuda.is_available() else 'cpu'

def preprocess(text: string) -> list:
    """
    This function converts raw text data into words and remove words with frequency less than 5
    :param text: [string] sequence of string
    :return: [list] list of words in raw data
    """
    # Replace punctuation with tokens so we can use them in our model
    text = text.lower()
    text = text.replace('.', ' <PERIOD> ')
    text = text.replace(',', ' <COMMA> ')
    text = text.replace('"', ' <QUOTATION_MARK> ')
    text = text.replace(';', ' <SEMICOLON> ')
    text = text.replace('!', ' <EXCLAMATION_MARK> ')
    text = text.replace('?', ' <QUESTION_MARK> ')
    text = text.replace('(', ' <LEFT_PAREN> ')
    text = text.replace(')', ' <RIGHT_PAREN> ')
    text = text.replace('--', ' <HYPHENS> ')
    text = text.replace('?', ' <QUESTION_MARK> ')
    text = text.replace('\n', ' <NEW_LINE> ')
    text = text.replace(':', ' <COLON> ')

    words = text.split()
    word_counts = Counter(words)
    trimmed_words = [word.lower() + '</w>' for word in words if word_counts[word] > 5]
    return trimmed_words

def show_learning(embeddings:  Any, vocab: Any, device: Any) -> None:
    '''
    This function uses embeddings from the provided model and randomly select some words from vocabulary. Then for each
    random word, it find similar words using cosine similarity. Finally it prints out top 6 similar words to randonly
    chosen words.
    :param model: Trained model
    :param vocab: vocabulary instance
    :param device:
    :return: None
    '''

    embed_vectors = embeddings.weight

    word_vocab = list(vocab.get_word_vocab())
    total = len(word_vocab)
    idxs = random.sample(range(total), 5)
    words = [ word_vocab[idx] for idx in idxs]
    valid_examples = torch.LongTensor([vocab.lookup_token(word) for word in words]).to(device)
    valid_vectors = embeddings(valid_examples)

    magnitudes = embed_vectors.pow(2).sum(dim=1).sqrt().unsqueeze(0)
    valid_similarities = torch.mm(valid_vectors, embed_vectors.t()) / magnitudes
    _, closest_idxs = valid_similarities.topk(6)

    valid_examples, closest_idxs = valid_examples.to('cpu'), closest_idxs.to('cpu')
    for ii, valid_idx in enumerate(valid_examples):
        closest_words = [vocab.lookup_index(idx.item()) for idx in closest_idxs[ii]][1:]
        print('Chosen word: '+vocab.lookup_index(valid_idx.item()) + " ----SIMILAR WORDS---- " + ', '.join(closest_words))
    print("...\n")


def word_analogy(words: list, embeddings, vocab, gram_model=False) -> string:
    """
    This function perform word analogy task to test how well word embeddings are trained.
    NOTE:  This function expects given words to be part of vocabulary if gram_model = False i.e if we are not using
            ngram model.
    :param gram_model: Boolean to check if we'll use ngram model or not
    :param vocab: Vocabulary of dataset
    :param words: list of three words such that ### words[0] is to words[1] as words[2] is to ? ###
    :param embeddings: trained embeddings from model
    :return: target word which fits the analogy best
    """
    words = [word.lower() + '</w>' for word in words]
    embed_vectors = embeddings.weight
    tokens = torch.LongTensor([vocab.lookup_token(ex) for ex in words]).to(device)
    vectors = embeddings(tokens)
    inp1 = (vectors[1] - vectors[0]) + vectors[2]
    inp2 = embed_vectors
    magnitudes = inp2.pow(2).sum(dim=1).sqrt().unsqueeze(0) * inp1.pow(2).sum(dim=0).sqrt().unsqueeze(0)
    similarities = torch.mm(inp1.unsqueeze(0), inp2.t()) / magnitudes
    val, idxs = similarities.topk(5)
    for id, v in zip(idxs.squeeze(), val.squeeze()):
        print(vocab.lookup_index(id.item()), v.item())

    target = vocab.lookup_index(idxs[0][0].item())
    return target


def sub_sampling(tokens: list, threshold=1e-5) -> list:
    """
    This function samples words from a defined probability distribution in order to counter imbalance of
    the rare and frequent words. Proposed probability is chances that a word will be discarded from training set.
    :param tokens: [list] dataset in integer form
    :param threshold: [float]
    :return: [list] subsampled training data
    """
    print("Running subsampling...")
    words_count = Counter(tokens)
    total_words = len(tokens)
    word_freq = {word: count / total_words for word, count in words_count.items()}
    word_prob = {word: 1 - np.sqrt(threshold / word_freq[word]) for word in words_count}  # Proposed Probability
    sampled_vocab = [word for word in tokens if random.random() < (1 - word_prob[word])]
    return sampled_vocab


def get_noise_dist(words: list) -> Tensor:
    '''
    This function returns noise distribution to find negative samples for a target word
    :param words: list of all words in dataset
    :return: probability distribution over all words
    '''
    counter = Counter(words)
    total = len(words)
    freqs = {word: count / total for word, count in counter.items()}
    word_freqs = np.array(sorted(freqs.values(), reverse=True))
    unigram_dist = word_freqs / word_freqs.sum()
    noise_dist = torch.from_numpy(unigram_dist ** (0.75) / np.sum(unigram_dist ** (0.75)))
    return noise_dist


def merge_vocab(pair: tuple, v_in: dict) -> dict:
    '''

    :param pair: a tuple of two strings e.g ('es', 't') or ('e', 'r')
    :param v_in: current vocabulary with space seperated words
    :return: new vocabulary with given pair as single string in all occurrence over the vocabulary
    e.g: v_in {'m o d e s t':12, 'f a s t e r': 34, ... }
         v_out { 'm o d est':12, 'f a s t er': 34, ....}
    '''
    v_out = {}
    ngram = re.escape(' '.join(pair))
    p = re.compile(r'(?<!\S)' + ngram + r'(?!\S)')

    for word in v_in:
        w_out = p.sub(''.join(pair), word)
        v_out[w_out] = v_in[word]

    return v_out


def get_pairs(vocab: dict) -> dict:
    '''

    :param vocab: dictionary containing words as key and their corresponding count as value
    :return: [dict] a dictionary containing pairs of string as key and their count as value.
    e.g: { ('a', 'n'): 1508,
             ('es', 't'): 527,
             ('e', 'r'): 1031, ... }
    '''
    pairs = dd(int)
    for word, frequency in vocab.items():
        symbols = word.split()

        for i in range(len(symbols) - 1):
            pairs[symbols[i], symbols[i + 1]] += frequency

    return pairs


class Vocabulary(object):
    def __init__(self, cnfig, token_to_idx=None, NGRAMS = False):

        self.config = cnfig
        if token_to_idx is None:
            token_to_idx = {}
        self._token_to_idx = token_to_idx
        self._idx_to_token = {idx: token for token, idx in self._token_to_idx.items()}
        self._ngram_vocab = None
        self._word_vocab = None
        self.NGRAMS = NGRAMS

    def to_serializable(self):
        return {'token_to_idx': self._token_to_idx}

    @classmethod
    def from_serializable(cls, contents):
        return cls(**contents)

    def add_token(self, token: string) -> int:
        if token in self._token_to_idx:
            index = self._token_to_idx[token]
        else:
            index = len(self._token_to_idx)
            self._token_to_idx[token] = index
            self._idx_to_token[index] = token
        return index

    def lookup_token(self, token: string) -> int:
        return self._token_to_idx[token]

    def create_vocab(self, words: list):
        '''
        This function uses Byte Pair Encoding to find ngrams in given words.
        :param words: list of words to create vocabulary from
        :return: None
        '''
        if self.NGRAMS:
            tokens = [" ".join(word) for word in words]  # space seperated words
            vocab = Counter(tokens)
            for i in range(self.config['NUM_MERGES']):
                pairs = get_pairs(vocab)
                if not pairs:
                    break
                best = max(pairs, key=pairs.get)
                vocab = merge_vocab(best, vocab)
            tokens = list(vocab.keys())
            vocab = list(string.ascii_lowercase)
            for token in tokens:
                vocab += token.split()
            vocab = set(vocab)  # ngrams from BPE algorithm
            word_in_ngram = set(
                [word for word in words if word in vocab])  # full words in ngram vocab
            whole_words = set(words) ^ (word_in_ngram)  # words not in ngram vocab
            # aplbhabets =
            self._ngram_vocab = list(set(vocab) ^ set(word_in_ngram))
            self._word_vocab = set(words)
            self._idx_to_token = {ii: word for ii, word in enumerate(list(vocab) + list(whole_words))}
            self._token_to_idx = {word: ii for ii, word in self._idx_to_token.items()}
        else:
            word_counts = Counter(words)
            # sorting the words from most to least frequent in text occurrence
            sorted_vocab = sorted(word_counts, key=word_counts.get, reverse=True)
            # create int_to_vocab dictionaries
            self._idx_to_token = {ii: word for ii, word in enumerate(sorted_vocab)}
            self._token_to_idx = {word: ii for ii, word in self._idx_to_token.items()}
            self._word_vocab = list(self._token_to_idx.keys())

    def lookup_index(self, index: int) -> string:
        if index not in self._idx_to_token:
            raise KeyError("the index (%d) is not in the Vocabulary" % index)
        return self._idx_to_token[index]

    def lookup_ngram(self, words: list) -> list:
        '''

        :param words: [list] list of words
        :return: [list] list of space seperated ngrams in words
        '''
        outputs = []
        vocab = self.get_ngram_vocab()
        for word in words:
            start, end = 0, len(word)
            cur_output = []
            # Look for grams with the longest possible length in ngram vocab
            while start < len(word) and start < end:
                if word[start:end] in vocab:
                    cur_output.append(word[start:end])
                    start = end
                    end = len(word)
                else:
                    end -= 1
            outputs.append(' '.join(cur_output))
        return outputs

    def get_vocab(self) -> list:
        '''
        :return: Combined vocabulary containing words and ngrams
        '''
        return list(self._token_to_idx.keys())

    def get_word_vocab(self) -> list:
        '''

        :return: list of words vocabulary
        '''
        return self._word_vocab

    def get_ngram_vocab(self) -> list:
        '''
        :return: list of ngram vocabulary. NOTE: this vocabulary always contains alphabets.
        '''
        return self._ngram_vocab

    def __str__(self):
        return "<Vocabulary(size=%d)>" % len(self)

    def __len__(self):
        return len(self._token_to_idx)

class Loader(object):
    def __init__(self, args, cnfg):
        self.args = args
        self.cnfg = cnfg

    def load(self, path: string) -> tuple:
        '''

        :param path: [string] path of data file to read from
        :return: ( list, Vocabulary) list of words in dataset and a Vocabulary class created using these words is
        returned
        '''
        file = open(path).read()
        words = preprocess(file)
        vocab = Vocabulary(self.cnfg, NGRAMS=self.args.NGRAMS)
        vocab.create_vocab(words)
        print('Vocabulary created')
        return words, vocab


class Dataset(Dataset):
    def __init__(self, args, config):
        self.args = args
        self.config = config
        self.loader = Loader(args, config)
        self.data_dict = dd(dd)
        self.n_max = None

        # Based on dataset statistics, not many examples length > 50
        print('Loading datasets...')
        path = args.DATA
        self.data_dict['data'], self.data_dict['vocab'] = self.loader.load(path)
        print('Data loaded !')
        self.data_dict['tokens'] = self.create_tokens()
        if args.NGRAMS:
            self.data_dict['ngram_tokens'] = self.create_ngram_token()
        print('Data preparation Completed !')

    def get_tokens(self) -> list:
        '''
        :param split: [string] (train,val,test)
        :return: [list] list of words for given split
        '''
        return self.data_dict['tokens']

    def get_ngram_tokens(self) -> list:
        '''
        :param split: [string] (train,val,test)
        :return: [list] list of words for given split
        '''
        return self.data_dict['ngram_tokens']

    def get_vocab_cls(self) -> Vocabulary:
        '''

        :param split: [string] (train,val,test)
        :return: [Vocabulary] Vocabulary class instance for given split
        '''
        return self.data_dict['vocab']

    def create_tokens(self) -> list:
        '''
        This function converts words from given split data into indices from vocabulary
        :param split: [string] (train,val,test)
        :return: [list] list of integers corresponding to indices of words in vocabulary for given split
        '''
        words = self.data_dict['data']
        vocab = self.get_vocab_cls()
        tokens = [vocab.lookup_token(word) for word in words]
        new_tokens = sub_sampling(tokens) if self.args.SUBSAMPLING else tokens
        return new_tokens

    def create_ngram_token(self) -> list:
        '''
        This function convert each word into list of indices of words and ngrams present in the word.
        e.g: say word is 'hello' with index 134 in vocabulary. Next we look for all possible ngrams of hello present in
        our ngram vocab. say we have 'he'(index:23) and 'lo'(index:48) in our ngram vocab. So we create a list of
        indices as [134, 23, 11, 48] which corresponds to [hello, he, l, lo]. We do this for all the words in given
        dataset and return a list of list.
        :param split: [string] (train,val,test)
        :return: [list] list of integers corresponding to indices of words and ngram in vocabulary for given split
        '''
        print("Ngrams processing...")
        vocab_cls = self.get_vocab_cls()
        words = [vocab_cls.lookup_index(token) for token in self.get_tokens()]
        # Using ngram vocabulary to look for ngrams in given word
        new_words = vocab_cls.lookup_ngram(words)
        ngram_tokens = [[vocab_cls.lookup_token(word)] + [vocab_cls.lookup_token(gram) for gram in n_word.split()] for
                        n_word, word in zip(new_words, words)]
        ngram_tokens, self.n_max = collate_fn_padd(ngram_tokens)
        return ngram_tokens

    def __getitem__(self, idx: int) -> tuple:
        """
        :param idx: [int] index for dataset object
        :return: [tuple] value at given index and a vocabulary object
        """
        if self.args.NGRAMS:
            return self.data_dict['ngram_tokens'][idx]
        else:
            return self.data_dict['tokens'][idx]

    def __len__(self):
        return self.data_dict['tokens'].__len__()


class DataLoader(object):
    def __init__(self,
                 dataset,
                 config,
                 NGRAMS=False,
                 shuffle=True
                 ):
        self.data = dataset
        self.config = config
        self.batch_size = self.config['BATCH_SIZE']
        self.shuffle = shuffle  # TO DO
        self.ngrams = NGRAMS

    def get_target(self, tokens, idx: int) -> list:
        """
        This function returns list of context words for a given target word from batch
        :param split: [int] type of data {train, val}
        :param idx: [int] index of target word in the batch
        :return: [list] list of c context words for given target word
        """

        c = np.random.randint(1, self.config['WINDOW_SIZE'] + 1)
        start = idx - c if (idx - c) > 0 else 0
        stop = idx + c
        target_words = tokens[start:idx] + tokens[idx + 1:stop + 1]
        return list(target_words)

    def get_batches(self):
        """
        It generate a batch of training data as pair of target and context word
        :return: [list] [list] list of target words and their corresponding context words
        """
        if self.ngrams:
            n_tokens = self.data.get_ngram_tokens()
            tokens = self.data.get_tokens()
            n_batches = len(tokens) // self.batch_size
            words = tokens[:n_batches * self.batch_size]
            for idx in range(0, len(words), self.batch_size):
                context_words, target_words = [], []
                batch = words[idx:idx + self.batch_size]
                for ii in range(len(batch)):
                    batch_x = n_tokens[:n_batches * self.batch_size][idx:idx + self.batch_size][ii]
                    batch_y = self.get_target(batch, ii)
                    target_words.extend(batch_y)
                    context_words.extend([batch_x] * len(batch_y))
                yield context_words, target_words
        else:
            tokens = self.data.get_tokens()
            n_batches = len(tokens) // self.batch_size
            words = tokens[:n_batches * self.batch_size]
            for idx in range(0, len(words), self.batch_size):
                context_words, target_words = [], []
                batch = words[idx:idx + self.batch_size]
                for ii in range(len(batch)):
                    batch_x = batch[ii]
                    batch_y = self.get_target(batch, ii)
                    target_words.extend(batch_y)
                    context_words.extend([batch_x] * len(batch_y))
                yield context_words, target_words


def collate_fn_padd(batch):
    '''
    Padds batch of variable length

    note: it converts things ToTensor manually here since the ToTensor transform
    assume it takes in images rather than arbitrary tensors.
    '''
    ## get sequence lengths
    lengths = torch.tensor([len(t) for t in batch]).to(device)
    ## padd
    batch = [torch.Tensor(t).to(device) for t in batch]
    batch = torch.nn.utils.rnn.pad_sequence(batch, batch_first=True)
    max_ = lengths.max().item()
    return batch.type(torch.int64), max_