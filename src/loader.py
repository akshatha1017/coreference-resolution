import torch
import torch.nn as nn
import torch.nn.functional as F
from torchtext.vocab import Vectors
from torch.autograd import Variable

import os, io, re, attr, random
from copy import deepcopy as c
from fnmatch import fnmatch
from cached_property import cached_property
from utils import *

NORMALIZE_DICT = {"/.": ".", "/?": "?",
                  "-LRB-": "(", "-RRB-": ")",
                  "-LCB-": "{", "-RCB-": "}",
                  "-LSB-": "[", "-RSB-": "]"}
REMOVED_CHAR = ["/", "%", "*"]


class Corpus:
    def __init__(self, documents):
        self.documents = documents
        self.vocab, self.char_vocab = self.get_vocab()

    def __getitem__(self, idx):
        return self.documents[idx]

    def __repr__(self):
        return 'Corpus containg %d documents' % len(self.documents)

    def get_vocab(self):
        """ Set vocabulary for LazyVectors """
        vocab, char_vocab = set(), set()

        for document in self.documents:
            vocab.update(document.tokens)
            char_vocab.update([char for word in document.tokens
                               for char in word])

        return vocab, char_vocab


class Document:
    def __init__(self, tokens, corefs, speakers, genre):
        self.tokens = tokens
        self.corefs = corefs
        self.speakers = speakers
        self.genre = genre

    def __getitem__(self, idx):
        return (self.tokens[idx], self.corefs[idx], self.speakers[idx], self.genre)

    def __repr__(self):
        return 'Document containing %d tokens' % len(self.tokens)

    def __len__(self):
        return len(self.tokens)

    @cached_property
    def spans(self):
        return compute_idx_spans(self.tokens)

    def truncate(self, MAX=50):
        """ Randomly truncate the document to up to MAX sentences """
        sentences = [idx for idx, token in enumerate(self.tokens) if token in ['.', '?', '!']]
        if len(sentences) > MAX:
            i = random.sample(range(MAX, len(sentences)), 1)[0]
            tokens = self.tokens[sentences[i-50]:sentences[i]]
            return self.__class__(tokens, c(self.corefs), c(self.speakers), c(self.genre))
        return self


@attr.s(frozen=True, repr=False)
class Span:

    # Left / right token indexes
    i1 = attr.ib()
    i2 = attr.ib()

    # Span embedding tensor
    g = attr.ib()

    # Speaker
    speaker = attr.ib(default=None)

    # Unary mention score, as tensor
    si = attr.ib(default=None)

    # List of candidate antecedent spans
    yi = attr.ib(default=None)

    # Pairwise scores for each yi
    sij = attr.ib(default=None)

    # Corresponding span ids to each yi
    yi_idx = attr.ib(default=None)

    def __repr__(self):
        return 'Span representing %d tokens' % (self.i2-self.i1+1)


class LazyVectors:
    """Load only those vectors from GloVE that are in the vocab.
    Assumes PAD id of 0 and UNK id of 1
    """

    unk_idx = 1

    def __init__(self, name='glove.840B.300d.txt',
                         cache='/Users/sob/github/.vector_cache/',
                         skim=None,
                         vocab=None):
        """  Requires the glove vectors to be in a folder named .vector_cache
        Setup:
            >> cd ~/where_you_want_to_save
            >> mkdir .vector_cache
            >> mv ~/where_glove_vectors_are_stored/glove.840B.300d.txt
                ~/where_you_want_to_save/.vector_cache/glove.840B.300d.txt
        Initialization (first init will be slow):
            >> VECTORS = LazyVectors(cache='~/where_you_saved_to/.vector_cache/',
                                     vocab_file='../path/vocabulary.txt',
                                     skim=None)
        Usage:
            >> weights = VECTORS.weights()
            >> embeddings = torch.nn.Embedding(weights.shape[0],
                                              weights.shape[1],
                                              padding_idx=0)
            >> embeddings.weight.data.copy_(weights)
            >> embeddings(sent_to_tensor('kids love unknown_word food'))
        You can access these moved vectors from any repository
        """
        self.__dict__.update(locals())
        if self.vocab is not None:
            self.set_vocab(vocab)

    @classmethod
    def from_corpus(cls, corpus_vocabulary):
        return cls(vocab=corpus_vocabulary)

    @cached_property
    def loader(self):
        return Vectors(self.name, cache=self.cache)

    def set_vocab(self, vocab):
        """ Set corpus vocab
        """
        # Intersects and initializes the torchtext Vectors class
        self.vocab = [v for v in vocab if v in self.loader.stoi][:self.skim]

        self.set_dicts()

    def get_vocab(self, filename):
        """ Read in vocabulary (top 30K words, covers ~93.5% of all tokens) """
        return read_file(filename)

    def set_dicts(self):
        """ _stoi: map string > index
            _itos: map index > string
        """
        self._stoi = {s: i for i, s in enumerate(self.vocab)}
        self._itos = {i: s for s, i in self._stoi.items()}

    def weights(self):
        """Build weights tensor for embedding layer """
        # Select vectors for vocab words.
        weights = torch.stack([
            self.loader.vectors[self.loader.stoi[s]]
            for s in self.vocab
        ])

        # Padding + UNK zeros rows.
        return torch.cat([
            torch.zeros((2, self.loader.dim)),
            weights,
        ])

    def stoi(self, s):
        """ String to index (s to i) for embedding lookup """
        idx = self._stoi.get(s)
        return idx + 2 if idx else self.unk_idx

    def itos(self, i):
        """ Index to string (i to s) for embedding lookup """
        token = self._itos.get(i)
        return token if token else 'UNK'


def read_corpus(dirname):
    conll_files = parse_filenames(dirname = dirname, pattern = "*gold_conll")
    return Corpus(flatten([load_file(file) for file in conll_files]))

def load_file(filename):
    """ Load a *._conll file
    Input:
        filename: path to the file
    Output:
        documents: list of Document class for each document in the file containing:
            tokens:                   split list of text
            utts_corefs:
                coref['label']:     id of the coreference cluster
                coref['start']:     start index (index of first token in the utterance)
                coref['end':        end index (index of last token in the utterance)
                coref['span']:      corresponding span
            utts_speakers:          list of speakers
            genre:                  genre of input
    """
    documents = []
    with io.open(filename, 'rt', encoding='utf-8', errors='strict') as f:
        tokens, text, utts_corefs, utts_speakers, corefs, index = [], [], [], [], [], 0
        genre = filename.split('/')[6]
        for line in f:
            cols = line.split()

            # End of utterance within a document: update lists, reset variables for next utterance.
            if len(cols) == 0:
                if text:
                    tokens.extend(text), utts_corefs.extend(corefs), utts_speakers.extend([speaker]*len(text))
                    text, corefs = [], []
                    continue

            # End of document: organize the data, append to output, reset variables for next document.
            elif len(cols) == 2:
                doc = fix_coref_spans(Document(tokens, utts_corefs, utts_speakers, genre))
                documents.append(doc)
                tokens, text, utts_corefs, utts_speakers, index = [], [], [], [], 0

            # Inside an utterance: grab text, speaker, coreference information.
            elif len(cols) > 7:
                text.append(clean_token(cols[3]))
                speaker = cols[9]

                # If the last column isn't a '-', there is a coreference link
                if cols[-1] != u'-':
                    coref_expr = cols[-1].split(u'|')
                    for token in coref_expr:

                        # Check if coref column token entry contains (, a number, or ).
                        match = re.match(r"^(\(?)(\d+)(\)?)$", token)
                        label = match.group(2)

                        # If it does, extract the coref label, its start index, and end index.
                        if match.group(1) == u'(':
                            corefs.append({'label': label, 'start': index, 'end': None, 'span': None})
                        if match.group(3) == u')':
                            for i in range(len(corefs)-1, -1, -1):
                                if corefs[i]['label'] == label and corefs[i]['end'] is None:
                                    break
                            corefs[i]['end'] = index
                index += 1
            else:
                # Beginning of document, beginning of file, end of file: nothing to scrape off
                continue

    return documents

def parse_filenames(dirname, pattern = "*conll"):
    """ Walk a nested directory to get all filename ending in a pattern """
    for path, subdirs, files in os.walk(dirname):
        for name in files:
            if fnmatch(name, pattern):
                yield os.path.join(path, name)

def clean_token(token):
    """ Substitute in /?(){}[] for equivalent CoNLL-2012 representations,
    remove /%* """
    cleaned_token = token
    if cleaned_token in NORMALIZE_DICT:
        cleaned_token = NORMALIZE_DICT[cleaned_token]

    if cleaned_token not in REMOVED_CHAR:
        for char in REMOVED_CHAR:
            cleaned_token = cleaned_token.replace(char, u'')

    if len(cleaned_token) == 0:
        cleaned_token = ","
    return cleaned_token

def token_to_id(token):
    """ Lookup word ID for a token """
    return VECTORS.stoi(token)

def doc_to_tensor(document):
    """ Convert a sentence to a tensor """
    return to_cuda(torch.tensor([token_to_id(token)
                                 for token in document.tokens]))

# Load in corpus, lazily load in word vectors.
train_corpus = read_corpus('../data/train/')
VECTORS = LazyVectors.from_corpus(train_corpus.vocab)
