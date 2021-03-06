import json
import logging
from typing import Dict, List

import numpy as np
from allennlp.common.file_utils import cached_path
from allennlp.data.dataset_readers.dataset_reader import DatasetReader
from allennlp.data.fields import (Field, LabelField, ListField, MetadataField,
                                  TextField)
from allennlp.data.instance import Instance
from allennlp.data.token_indexers import SingleIdTokenIndexer, TokenIndexer
from allennlp.data.tokenizers import Tokenizer, WordTokenizer
from allennlp.data.tokenizers.sentence_splitter import SpacySentenceSplitter
from overrides import overrides

logger = logging.getLogger(__name__)  # pylint: disable=invalid-name


@DatasetReader.register("semisupervised_text_classification_json")
class TextCatReader(DatasetReader):
    """
    Reads tokens and their labels from a labeled text classification dataset.
    Expects a "tokens" field and a "category" field in JSON format.

    The output of ``read`` is a list of ``Instance`` s with the fields:
        tokens: ``TextField`` and
        label: ``LabelField``

    Parameters
    ----------
    token_indexers : ``Dict[str, TokenIndexer]``, optional
        optional (default=``{"tokens": SingleIdTokenIndexer()}``)
        We use this to define the input representation for the text.
        See :class:`TokenIndexer`.
    tokenizer : ``Tokenizer``, optional (default = ``{"tokens": WordTokenizer()}``)
        Tokenizer to use to split the input text into words or other kinds of tokens.
    segment_sentences: ``bool``, optional (default = ``False``)
        If True, we will first segment the text into sentences using SpaCy and then tokenize words.
        Necessary for some models that require pre-segmentation of sentences,
        like the Hierarchical Attention Network.
    sequence_length: ``int``, optional (default = ``None``)
        If specified, will truncate tokens to specified maximum length.
    ignore_labels: ``bool``, optional (default = ``False``)
        If specified, will ignore labels when reading data, useful for semi-supervised textcat
    skip_label_indexing: ``bool``, optional (default = ``False``)
        Whether or not to skip label indexing. You might want to skip label indexing if your
        labels are numbers, so the dataset reader doesn't re-number them starting from 0.
    lazy : ``bool``, optional, (default = ``False``)
        Whether or not instances can be read lazily.
    """
    def __init__(self,
                 token_indexers: Dict[str, TokenIndexer] = None,
                 tokenizer: Tokenizer = None,
                 unrestricted_tokenizer: Tokenizer = None,
                 segment_sentences: bool = False,
                 sequence_length: int = None,
                 ignore_labels: bool = False,
                 skip_label_indexing: bool = False,
                 sample: int = None,
                 unlabeled_data_path: str = None,
                 lazy: bool = False) -> None:
        super().__init__(lazy=lazy)
        self._tokenizer = tokenizer or WordTokenizer()
        self._unrestricted_tokenizer = unrestricted_tokenizer
        self._sample = sample
        self._segment_sentences = segment_sentences
        self._sequence_length = sequence_length
        self._ignore_labels = ignore_labels
        self._skip_label_indexing = skip_label_indexing
        self._token_indexers = token_indexers or {'tokens': SingleIdTokenIndexer()}
        self._unlabeled_data_path = unlabeled_data_path
        if self._segment_sentences:
            self._sentence_segmenter = SpacySentenceSplitter()

    def _reservoir_sampling(self, file_):
        """
        reservoir sampling for reading random lines from file without loading
        entire file into memory

        See here for explanation of algorithm:
        https://stackoverflow.com/questions/35680236/select-100-random-lines-from-a-file-with-a-1-million-which-cant-be-read-into-me

        Parameters
        ----------
        file : `str` - file path
        sample_size : `int` - size of random sample you want

        Returns
        -------
        result : `List[str]` - sample lines of file
        """
        file_iterator = iter(file_)

        try:
            result = [next(file_iterator) for _ in range(self._sample)]

        except StopIteration:
            raise ValueError("Sample larger than population")

        for index, item in enumerate(file_iterator, start=self._sample):
            sample_index = np.random.randint(0, index)
            if sample_index < self._sample:
                result[sample_index] = item

        np.random.shuffle(result)

        return result

    @overrides
    def _read(self, file_path):
        with open(cached_path(file_path), "r") as data_file:
            if self._sample is not None:
                lines = [(item, False) for item in self._reservoir_sampling(data_file)]
            else:
                lines = [(item, True) for item in data_file.readlines()]

        if self._unlabeled_data_path:
            with open(cached_path(self._unlabeled_data_path)) as data_file:
                lines += [(item, False) for item in data_file.readlines()]

        for line, is_labeled in lines:
            items = json.loads(line)
            text = items["tokens"]
            label = str(items['category'])
            instance = self.text_to_instance(text=text, label=label, is_labeled=is_labeled)
            if instance is not None:
                yield instance

    def _truncate(self, tokens):
        """
        truncate a set of tokens using the provided sequence length
        """
        if len(tokens) > self._sequence_length:
            tokens = tokens[:self._sequence_length]
        return tokens

    @overrides
    def text_to_instance(self, text: str, label: str = None, is_labeled: bool = False) -> Instance:  # type: ignore
        """
        Parameters
        ----------
        text : ``str``, required.
            The text to classify
        label ``str``, optional, (default = None).
            The label for this text.

        Returns
        -------
        An ``Instance`` containing the following fields:
            tokens : ``TextField``
                The tokens in the sentence or phrase.
            label : ``LabelField``
                The label label of the sentence or phrase.
        """
        # pylint: disable=arguments-differ
        fields: Dict[str, Field] = {}
        if self._segment_sentences:
            sentences: List[Field] = []
            sentence_splits = self._sentence_segmenter.split_sentences(text)
            for sentence in sentence_splits:
                word_tokens = self._tokenizer.tokenize(sentence)
                if self._sequence_length is not None:
                    word_tokens = self._truncate(word_tokens)
                sentences.append(TextField(word_tokens, self._token_indexers))
            fields['tokens'] = ListField(sentences)
        else:

            tokens = self._tokenizer.tokenize(text)
            if self._sequence_length is not None:
                tokens = self._truncate(tokens)

            fields['tokens'] = TextField(tokens, self._token_indexers)

            if self._unrestricted_tokenizer:
                unrestricted_tokens = self._unrestricted_tokenizer.tokenize(text)
                if self._sequence_length is not None:
                    unrestricted_tokens = self._truncate(unrestricted_tokens)
                fields['filtered_tokens'] = TextField(unrestricted_tokens, self._token_indexers)

        # TODO: Document 'default' unsupervised label as pre-condition.
        if label is not None:
            fields['label'] = LabelField(label, skip_indexing=self._skip_label_indexing)
        fields['metadata'] = MetadataField({"is_labeled": is_labeled})

        return Instance(fields)
