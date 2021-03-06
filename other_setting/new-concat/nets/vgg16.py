# --------------------------------------------------------
# Tensorflow Faster R-CNN
# Licensed under The MIT License [see LICENSE for details]
# Written by Xinlei Chen
# --------------------------------------------------------
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import tensorflow as tf
import tensorflow.contrib.slim as slim
from tensorflow.contrib.slim import losses
from tensorflow.contrib.slim import arg_scope
import numpy as np

from nets.network import Network
from model.config import cfg

factor1 = cfg.SCORE_FACTOR1
factor2 = cfg.SCORE_FACTOR2

#threshold
rpn3_reject = cfg.RPN_REJECT3
rpn2_reject = cfg.RPN_REJECT2
rpn1_reject = cfg.RPN_REJECT1

reject3 = cfg.REJECT3
reject2 = cfg.REJECT2
reject1 = cfg.REJECT1

#factor
rpn3_reject_f = cfg.RPN_REJECT3_FACTOR
rpn2_reject_f = cfg.RPN_REJECT2_FACTOR
rpn1_reject_f = cfg.RPN_REJECT1_FACTOR

reject3_f = cfg.REJECT3_FACTOR
reject2_f = cfg.REJECT2_FACTOR
reject1_f = cfg.REJECT1_FACTOR

#batch
rpn_batch3 = cfg.TRAIN.RPN_BATCH3
rpn_batch2 = cfg.TRAIN.RPN_BATCH2
rpn_batch1 = cfg.TRAIN.RPN_BATCH1
rpn_batch = cfg.TRAIN.RPN_BATCH

batch3 = cfg.TRAIN.BATCH3
batch2 = cfg.TRAIN.BATCH2
batch1 = cfg.TRAIN.BATCH1
batch = cfg.TRAIN.BATCH

#OHEM
OHEM3 = cfg.TRAIN.OHEM3
OHEM2 = cfg.TRAIN.OHEM2
OHEM1 = cfg.TRAIN.OHEM1
OHEM = cfg.TRAIN.OHEM


class vgg16(Network):
  def __init__(self, batch_size=1):
    Network.__init__(self, batch_size=batch_size)

    self.endpoint = {}

  def build_network(self, sess, is_training=True):
    with tf.variable_scope('vgg_16', 'vgg_16'):
      # select initializers
      if cfg.TRAIN.TRUNCATED:
        initializer = tf.truncated_normal_initializer(mean=0.0, stddev=0.01)
        initializer_bbox = tf.truncated_normal_initializer(mean=0.0, stddev=0.001)
      else:
        initializer = tf.random_normal_initializer(mean=0.0, stddev=0.01)
        initializer_bbox = tf.random_normal_initializer(mean=0.0, stddev=0.001)

      net = slim.repeat(self._image, 2, slim.conv2d, 64, [3, 3],
                        trainable=False, scope='conv1')
      net = slim.max_pool2d(net, [2, 2], padding='SAME', scope='pool1')
      net = slim.repeat(net, 2, slim.conv2d, 128, [3, 3],
                        trainable=False, scope='conv2')
      net = slim.max_pool2d(net, [2, 2], padding='SAME', scope='pool2')
      net = slim.repeat(net, 3, slim.conv2d, 256, [3, 3],
                        trainable=is_training, scope='conv3')


      net = slim.max_pool2d(net, [2, 2], padding='SAME', scope='pool3')
      net = slim.repeat(net, 3, slim.conv2d, 512, [3, 3],
                        trainable=is_training, scope='conv4')

      #store conv4_3
      self.endpoint['conv4_3'] = net

      #continue conv5
      net = slim.max_pool2d(net, [2, 2], padding='SAME', scope='pool4')
      net = slim.repeat(net, 3, slim.conv2d, 512, [3, 3],
                        trainable=is_training, scope='conv5')

      #store conv5_3
      self.endpoint['conv5_3'] = net
      self._layers['head'] = self.endpoint['conv5_3']

      # build the anchors for the image
      self._anchor_component()




      ##-----------------------------------------------rpn-----------------------------------------------------------------##
      rpn = slim.conv2d(self.endpoint['conv5_3'], 512, [3, 3], trainable=is_training, weights_initializer=initializer, scope="rpn_conv/3x3")

      self._act_summaries.append(rpn)

      rpn_cls_score = slim.conv2d(rpn, self._num_anchors * 2, [1, 1], trainable=is_training,
                                  weights_initializer=initializer,
                                  padding='VALID', activation_fn=None, scope='rpn_cls_score_pre')

      rpn_bbox_pred = slim.conv2d(rpn, self._num_anchors * 4, [1, 1], trainable=is_training,
                                  weights_initializer=initializer,
                                  padding='VALID', activation_fn=None, scope='rpn_bbox_pred')


      # rpn_cls_score = rpn3_cls_score*rpn3_cls_score_scale*0.25 + rpn2_cls_score*rpn2_cls_score_scale*0.25 + rpn1_cls_score*rpn1_cls_score_scale*0.25 + rpn0_cls_score*rpn0_cls_score_scale*0.25

      #used added up score
      rpn_cls_score_reshape = self._reshape_layer(rpn_cls_score, 2, 'rpn_cls_score_reshape')
      rpn_cls_prob_reshape = self._softmax_layer(rpn_cls_score_reshape, "rpn_cls_prob_reshape")
      rpn_cls_prob = self._reshape_layer(rpn_cls_prob_reshape, self._num_anchors * 2, "rpn_cls_prob")


      if is_training:
        #compute anchor loss
        rpn_labels = self._anchor_target_layer(rpn_cls_score, "anchor", [], [], rpn_batch, OHEM)

      ######################################################RPN DONE##################################################################

      #---------------------------------------------------porposal is made here------------------------------------------------------#

      if is_training:
        # #compute anchor loss
        # rpn_labels = self._anchor_target_layer(rpn_cls_score, "anchor", rpn1_reject_inds)
        rois, roi_scores = self._proposal_layer(rpn_cls_prob, rpn_bbox_pred, "rois", [], [])

        # with tf.control_dependencies([rpn_labels]):
        #   rois, _ = self._proposal_target_layer(rois, roi_scores, "rpn_rois")

      else:
        if cfg.TEST.MODE == 'nms':
          rois, _ = self._proposal_layer(rpn_cls_prob, rpn_bbox_pred, "rois", [], [])
        elif cfg.TEST.MODE == 'top':
          rois, _ = self._proposal_top_layer(rpn_cls_prob, rpn_bbox_pred, "rois", [], [])
        else:
          raise NotImplementedError

      #----------------------------------------------------------finish proposal-----------------------------------------------------#



      #############################################################RCNN START###############################################################

      # make target for training 
      if is_training:
       with tf.control_dependencies([rpn_labels]):
         rois, _, passinds3 = self._proposal_target_layer(rois, roi_scores, "rpn_rois", batch)

      #------------------------------------------------------rcnn 2----------------------------------------------------#

      if cfg.POOLING_MODE == 'crop':
        pool41 = self._crop_pool_layer(self.endpoint['conv4_3'], rois, 8, 7, "pool41")
      else:
        raise NotImplementedError

      pool41_avg = slim.avg_pool2d(pool41, [7, 7], padding='SAME', scope='pool41_avg', stride = 1) 
      pool41_flat = slim.flatten(pool41_avg, scope='flatten41')
      pool41_norm = self.l2_norm(pool41_flat, 1, 30.0, 'pool41_norm', is_training)
      #pool41_norm = self.normalize_to_target(pool41_flat, 10.0, 1, 'pool41_norm')



      if cfg.POOLING_MODE == 'crop':
        pool5 = self._crop_pool_layer(self.endpoint['conv5_3'], rois, 16, 7, "pool5")
        self.endpoint['pool5'] = pool5
      else:
        raise NotImplementedError

      pool_avg = slim.avg_pool2d(pool5, [7, 7], padding='SAME', scope='pool_avg', stride = 1) 
      pool_avg_flat = slim.flatten(pool_avg, scope='pool_avg_flat')
      pool_norm = self.l2_norm(pool_avg_flat, 1, 30.0, 'pool_norm', is_training)
      #pool_norm = self.normalize_to_target(pool_avg_flat, 10.0, 1, 'pool_norm')

      pool_flat = slim.flatten(pool5, scope='flatten')

      fc6 = slim.fully_connected(pool_flat, 4096, scope='fc6')
      if is_training:
        fc6 = slim.dropout(fc6, keep_prob=0.5, is_training=True, scope='dropout6')
      fc7 = slim.fully_connected(fc6, 4096, scope='fc7')
      if is_training:
        fc7 = slim.dropout(fc7, keep_prob=0.5, is_training=True, scope='dropout7')

      self._predictions['fc7'] = fc7
      fc7_norm = self.l2_norm(fc7, 1, 30.0, 'fc7_norm', is_training)
      #fc7_norm = self.normalize_to_target(fc7, 10.0, 1, 'fc7_norm')

      fc7_concat = tf.concat([fc7_norm, pool_norm, pool41_norm], 1, 'fc7_concat')

      cls_score = slim.fully_connected(fc7_concat, self._num_classes,
                                       weights_initializer=initializer,
                                       trainable=is_training,
                                       activation_fn=None, scope='cls_score')


      bbox_pred = slim.fully_connected(fc7_concat, self._num_classes * 4,
                                       weights_initializer=initializer_bbox,
                                       trainable=is_training,
                                       activation_fn=None, scope='bbox_pred')

      cls_prob = self._softmax_layer(cls_score, "cls_prob")


      self._act_summaries.append(self.endpoint['conv5_3'])
      ###########################################################RCNN DONE############################################################

      #store rpn values
      self._predictions["rpn_cls_score"] = rpn_cls_score
      self._predictions["rpn_cls_score_reshape"] = rpn_cls_score_reshape
      self._predictions["rpn_cls_prob"] = rpn_cls_prob
      self._predictions["rpn_bbox_pred"] = rpn_bbox_pred


      #store RCNN
      self._predictions["cls_score"] = cls_score
      self._predictions["cls_prob"] = cls_prob
      self._predictions["bbox_pred"] = bbox_pred
      self._predictions["rois"] = rois
      #####only for training######


      self._score_summaries.update(self._predictions)

      return rois, cls_prob, bbox_pred


  def get_variables_to_restore(self, variables, var_keep_dic):
    variables_to_restore = []

    for v in variables:
      # exclude the conv weights that are fc weights in vgg16
      if v.name == 'vgg_16/fc6/weights:0' or v.name == 'vgg_16/fc7/weights:0':
        self._variables_to_fix[v.name] = v
        continue
      # exclude the first conv layer to swap RGB to BGR
      if v.name == 'vgg_16/conv1/conv1_1/weights:0':
        self._variables_to_fix[v.name] = v
        continue


      if v.name.split(':')[0] in var_keep_dic:
        print('Varibles restored: %s' % v.name)
        variables_to_restore.append(v)

    return variables_to_restore

  def fix_variables(self, sess, pretrained_model):
    print('Fix VGG16 layers..')
    with tf.variable_scope('Fix_VGG16') as scope:
      with tf.device("/cpu:0"):
        # fix the vgg16 issue from conv weights to fc weights
        # fix RGB to BGR
        fc6_conv = tf.get_variable("fc6_conv", [7, 7, 512, 4096], trainable=False)

        fc7_conv = tf.get_variable("fc7_conv", [1, 1, 4096, 4096], trainable=False)

        conv1_rgb = tf.get_variable("conv1_rgb", [3, 3, 3, 64], trainable=False)
        restorer_fc = tf.train.Saver({"vgg_16/fc6/weights": fc6_conv, 
                                      "vgg_16/fc7/weights": fc7_conv,
                                      "vgg_16/conv1/conv1_1/weights": conv1_rgb})
        restorer_fc.restore(sess, pretrained_model)

        sess.run(tf.assign(self._variables_to_fix['vgg_16/fc6/weights:0'], tf.reshape(fc6_conv, 
                            self._variables_to_fix['vgg_16/fc6/weights:0'].get_shape())))
        sess.run(tf.assign(self._variables_to_fix['vgg_16/fc7/weights:0'], tf.reshape(fc7_conv, 
                            self._variables_to_fix['vgg_16/fc7/weights:0'].get_shape())))


        sess.run(tf.assign(self._variables_to_fix['vgg_16/conv1/conv1_1/weights:0'], 
                            tf.reverse(conv1_rgb, [2])))
