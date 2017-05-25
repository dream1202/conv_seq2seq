# Copyright 2017 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
Base class for sequence decoders.
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import abc
from collections import namedtuple
from pydoc import locate

import six
import tensorflow as tf
from tensorflow.python.util import nest  # pylint: disable=E0611
from tensorflow.python.framework import dtypes
from tensorflow.python.ops import math_ops

from seq2seq.graph_module import GraphModule
from seq2seq.configurable import Configurable
from seq2seq.contrib.seq2seq.decoder import Decoder, dynamic_decode
from seq2seq.contrib.seq2seq.decoder import _transpose_batch_time
from seq2seq.encoders.pooling_encoder import _create_position_embedding, position_encoding
from seq2seq.encoders.conv_encoder_utils import ConvEncoderUtils
from seq2seq.inference import beam_search  

class ConvDecoderOutput(
    #namedtuple("ConvDecoderOutput", ["logits", "predicted_ids", "cell_output", "attention_scores", "attention_context"])):
    namedtuple("ConvDecoderOutput", ["logits", "predicted_ids"])): 
    pass


@six.add_metaclass(abc.ABCMeta)
class ConvDecoder(GraphModule, Configurable):
  """An RNN Decoder that uses attention over an input sequence.

  Args:
    cell: An instance of ` tf.contrib.rnn.RNNCell`
    helper: An instance of `tf.contrib.seq2seq.Helper` to assist decoding
    initial_state: A tensor or tuple of tensors used as the initial cell
      state.
    vocab_size: Output vocabulary size, i.e. number of units
      in the softmax layer
    attention_keys: The sequence used to calculate attention scores.
      A tensor of shape `[B, T, ...]`.
    attention_values: The sequence to attend over.
      A tensor of shape `[B, T, input_dim]`.
    attention_values_length: Sequence length of the attention values.
      An int32 Tensor of shape `[B]`.
    attention_fn: The attention function to use. This function map from
      `(state, inputs)` to `(attention_scores, attention_context)`.
      For an example, see `seq2seq.decoder.attention.AttentionLayer`.
    reverse_scores: Optional, an array of sequence length. If set,
      reverse the attention scores in the output. This is used for when
      a reversed source sequence is fed as an input but you want to
      return the scores in non-reversed order.
  """

  def __init__(self,
               params,
               mode,
               vocab_size,
               attention_keys,
               attention_values,
               attention_values_length,
               attention_fn,
               reverse_scores_lengths=None,
               name="conv_decoder_fairseq"):
    GraphModule.__init__(self, name)
    Configurable.__init__(self, params, mode)
    
    self.vocab_size = vocab_size
    self.attention_keys = attention_keys
    self.attention_values = attention_values
    self.attention_values_length = attention_values_length
    self.attention_fn = attention_fn
    self.reverse_scores_lengths = reverse_scores_lengths
    
    self._combiner_fn = locate(self.params["position_embeddings.combiner_fn"])

  @staticmethod
  def default_params():
    return {
        "cnn.layers": 3,
        "cnn.nhids": "512,512,512",
        "cnn.kwidths": "3,3,3",
        "cnn.nhid_default": 256,
        "cnn.kwidth_default": 3,
        "embedding_dropout_keep_prob": 0.8,
        "nhid_dropout_keep_prob": 0.8,
        "out_dropout_keep_prob": 0.8,
        "word_embeddings.size": 512,
        "position_embeddings.enable": True,
        "position_embeddings.combiner_fn": "tensorflow.add",
        "position_embeddings.num_positions": 100,
        "init_scale": 0.04,
        "nout_embed": 256,
    }
  
  def compute_output(self, cell_output):
    """Computes the decoder outputs."""

    # Compute attention
    att_scores, attention_context = self.attention_fn(
        query=cell_output,
        keys=self.attention_keys,
        values=self.attention_values,
        values_length=self.attention_values_length)

    # TODO: Make this a parameter: We may or may not want this.
    # Transform attention context.
    # This makes the softmax smaller and allows us to synthesize information
    # between decoder state and attention context
    # see https://arxiv.org/abs/1508.04025v5
    softmax_input = tf.contrib.layers.fully_connected(
        inputs=tf.concat([cell_output, attention_context], 1),
        num_outputs=self.cell.output_size,
        activation_fn=tf.nn.tanh,
        scope="attention_mix")

    # Softmax computation
    logits = tf.contrib.layers.fully_connected(
        inputs=softmax_input,
        num_outputs=self.vocab_size,
        activation_fn=None,
        scope="logits")

    return softmax_input, logits, att_scores, attention_context



  def conv_decoder_train(self, decoder, enc_output, labels, sequence_length):
    """
    if not isinstance(decoder, Decoder):
      raise TypeError("Expected decoder to be type Decoder, but saw: %s" %
                      type(decoder))
    """
    embed_size = labels.get_shape().as_list()[-1]
    if self.params["position_embeddings.enable"]:
      positions_embed = _create_position_embedding(
          embedding_dim=embed_size,
          num_positions=self.params["position_embeddings.num_positions"],
          lengths=sequence_length,
          maxlen=tf.shape(labels)[1])
      labels = self._combiner_fn(labels, positions_embed)
     
    # Apply dropout to embeddings
    inputs = tf.contrib.layers.dropout(
        inputs=labels,
        keep_prob=self.params["embedding_dropout_keep_prob"],
        is_training=self.mode == tf.contrib.learn.ModeKeys.TRAIN)
    
    with tf.variable_scope("decoder_cnn"):    
      next_layer = inputs
      if self.params["cnn.layers"] > 0:
        nhids_list = ConvEncoderUtils.parse_list_or_default(self.params["cnn.nhids"], self.params["cnn.layers"], self.params["cnn.nhid_default"])
        kwidths_list = ConvEncoderUtils.parse_list_or_default(self.params["cnn.kwidths"], self.params["cnn.layers"], self.params["cnn.kwidth_default"])
        
        # mapping emb dim to hid dim
        next_layer = ConvEncoderUtils.linear_mapping(next_layer, nhids_list[0], dropout=self.params["embedding_dropout_keep_prob"], var_scope_name="linear_mapping_before_cnn")      
         
        next_layer = ConvEncoderUtils.conv_decoder_stack(inputs, enc_output, next_layer, nhids_list, kwidths_list, {'src':0.8, 'hid':0.8}, mode=self.mode)
    
    with tf.variable_scope("softmax"):
      next_layer = ConvEncoderUtils.linear_mapping(next_layer, self.params["nout_embed"], var_scope_name="linear_mapping_after_cnn")
      next_layer = tf.contrib.layers.dropout(
        inputs=next_layer,
        keep_prob=self.params["out_dropout_keep_prob"],
        is_training=self.mode == tf.contrib.learn.ModeKeys.TRAIN)
      
      next_layer = ConvEncoderUtils.linear_mapping(next_layer, self.vocab_size, dropout=self.params["out_dropout_keep_prob"], var_scope_name="logits_before_softmax")
       
    logits = _transpose_batch_time(next_layer)   

    sample_ids = tf.cast(tf.argmax(logits, axis=-1), tf.int32)
    #sample_ids = math_ops.cast(math_ops.argmax(logits, axis=-1), dtypes.int32)
 
    return ConvDecoderOutput(logits=logits, predicted_ids=sample_ids)
    

  def infer_conv_block(self, enc_output, input_embed):
    # Apply dropout to embeddings
    input_embed = tf.contrib.layers.dropout(
        inputs=input_embed,
        keep_prob=self.params["embedding_dropout_keep_prob"],
        is_training=self.mode == tf.contrib.learn.ModeKeys.INFER)
     
    with tf.variable_scope("decoder_cnn"):    
      next_layer = input_embed
      if self.params["cnn.layers"] > 0:
        nhids_list = ConvEncoderUtils.parse_list_or_default(self.params["cnn.nhids"], self.params["cnn.layers"], self.params["cnn.nhid_default"])
        kwidths_list = ConvEncoderUtils.parse_list_or_default(self.params["cnn.kwidths"], self.params["cnn.layers"], self.params["cnn.kwidth_default"])
        
        # mapping emb dim to hid dim
        next_layer = ConvEncoderUtils.linear_mapping(next_layer, nhids_list[0], dropout=self.params["embedding_dropout_keep_prob"], var_scope_name="linear_mapping_before_cnn")      
         
        next_layer = ConvEncoderUtils.conv_decoder_stack(inputs, enc_output, next_layer, nhids_list, kwidths_list, {'src':0.8, 'hid':0.8}, mode=self.mode)
    
    with tf.variable_scope("softmax"):
      next_layer = ConvEncoderUtils.linear_mapping(next_layer[:,-1:,:], self.params["nout_embed"], var_scope_name="linear_mapping_after_cnn")
      next_layer = tf.contrib.layers.dropout(
        inputs=next_layer,
        keep_prob=self.params["out_dropout_keep_prob"],
        is_training=self.mode == tf.contrib.learn.ModeKeys.INFER)
      
      next_layer = ConvEncoderUtils.linear_mapping(next_layer, self.vocab_size, dropout=self.params["out_dropout_keep_prob"], var_scope_name="logits_before_softmax")
    
    shape = next_layer.get_shape().as_list()  
    logits = tf.reshape(next_layer, [shape[0],shape(-1)])   
    return logits


  def create_init_state(self, config, target_embedding, start_tokens)
    
    beam_state = beam_search.create_initial_beam_state(config)    
    
    start_embedded = tf.nn.embedding_lookup(target_embedding, start_tokens)

    embed_size = start_embedded.get_shape().as_list()[-1]
    if self.params["position_embeddings.enable"]:
      positions_embed = tf.constant(position_encoding(self.params["position_embeddings.num_positions"], embed_size), name="position_encoding") 
        
    
    start_pos_embed = positions_embed[0:1,:]
    start_pos_embed_batch = tf.tile(start_pos_embed, [config.beam_size,1])
    
    input_embed = self._combiner_fn(start_embedded + start_pos_embed)
    return input_embed, beam_state
 
  def next_inputs(self, time, outputs, state, sample_ids, name=None):
    finished = math_ops.equal(sample_ids, self._end_token)
    all_finished = math_ops.reduce_all(finished)
    next_inputs = control_flow_ops.cond(
        all_finished,
        # If we're finished, the next_inputs value doesn't matter
        lambda: self._start_inputs,
        lambda: self._embedding_fn(sample_ids))
    return finished, next_inputs



  def step(self, time_, inputs, beam_state, name=None):
    
    logits = self.infer_conv_block(enc_output, inputs)
    
    bs_output, beam_state = beam_search.beam_search_step(
        time_=time_,
        logits=logits,
        beam_state=beam_state,
        config=config)

    finished, next_inputs = self.next_inputs(sample_ids=bs_output.predicted_ids)
    next_inputs.set_shape([self.batch_size, None])

    return bs_output, next_inputs, beam_state
    
  def conv_decoder_infer(self, decoder, enc_output, target_embedding, start_tokens, config):

    inputs, beam_state = create_init_state(config, target_embedding, start_tokens)
    
    time_ = 0
        

    WHILE:
        bs_output, next_inputs, beam_state = self.step(time_, inputs, beam_state) 
        inputs = create_next_inputs(inputs, next_inputs)
         
        outputs = BeamDecoderOutput(
        logits=tf.zeros([self.config.beam_width, self.config.vocab_size]),
        predicted_ids=bs_output.predicted_ids,
        log_probs=beam_state.log_probs,
        scores=bs_output.scores,
        beam_parent_ids=bs_output.beam_parent_ids,
        original_outputs=decoder_output)
    
    
      outputs_ta = nest.map_structure(lambda ta, out: ta.write(time, out),
                                      outputs_ta, outputs)
    final_outputs = nest.map_structure(lambda ta: ta.stack(), final_outputs_ta)

    sample_ids = tf.cast(tf.argmax(logits, axis=-1), tf.int32)
    #sample_ids = math_ops.cast(math_ops.argmax(logits, axis=-1), dtypes.int32)
 
    return ConvDecoderOutput(logits=logits, predicted_ids=sample_ids)





  def _build(self, enc_output, labels=None, sequence_length=None, target_embedding=None, start_tokens=None, config=None):

    #scope = tf.get_variable_scope()
    #scope.set_initializer(tf.random_uniform_initializer(-self.params["init_scale"], self.params["init_scale"]))

    maximum_iterations = None
    if self.mode == tf.contrib.learn.ModeKeys.INFER:
      maximum_iterations = self.params["max_decode_length"]
      outputs = self.conv_decoder_infer(decoder=self, enc_output=enc_output, target_embedding=target_embedding, start_tokens=start_tokens, config=config)
    else:
      outputs = self.conv_decoder_train(decoder=self, enc_output=enc_output, labels=labels, sequence_length=sequence_length)
    return outputs, outputs
