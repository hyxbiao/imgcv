from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import sys

import logging
import argparse
import numpy as np
import pandas as pd
import tensorflow as tf  # pylint: disable=g-bad-import-order
from imgcv.dataset import DataSet
from imgcv import classification as cls
from imgcv.models import vgg
from imgcv.utils import preprocess as pp
import web
from tornado import gen

_RESIZE_MIN = 256

_R_MEAN = 123.68
_G_MEAN = 116.78
_B_MEAN = 103.94
_CHANNEL_MEANS = [_R_MEAN, _G_MEAN, _B_MEAN]


HEIGHT = 224
WIDTH = 224
NUM_CHANNELS = 3
_SHUFFLE_BUFFER = 1500


class FashionAIDataSet(DataSet):
    CSV_TYPES = [[''], [''], ['']]
    CSV_COLUMN_NAMES = ['image', 'key', 'value']

    def __init__(self, flags):
        super(FashionAIDataSet, self).__init__(flags)

        df = self.load_meta_data(tf.estimator.ModeKeys.TRAIN)
        self._num_classes = len(df['value'].value_counts())
        self.train_df = df.sample(frac=0.9, random_state=1)
        self.test_df = df.drop(self.train_df.index)

    @property
    def num_classes(self):
        return self._num_classes

    @property
    def train_num_images(self):
        return len(self.train_df)

    @property
    def test_num_images(self):
        return len(self.test_df)

    def load_meta_data(self, mode, convert=True):
        metas = self.get_metas(mode, self.flags.data_dir)
        dfs = []
        for data_dir, meta_file in metas:
            if convert:
                converters = {
                    'image': lambda x, data_dir=data_dir: os.path.join(data_dir, *x.split('/')),
                }
                if mode != tf.estimator.ModeKeys.PREDICT:
                    converters['value'] = lambda x: x.index('y')
            else:
                converters = None
            df = pd.read_csv(meta_file, names=self.CSV_COLUMN_NAMES, header=0, converters=converters)
            df = df[df['key'] == self.flags.attr_key]
            dfs.append(df)
        df = pd.concat(dfs)
        return df

    def debug_fn(self):
        if self.flags.predict:
            mode = tf.estimator.ModeKeys.PREDICT
        else:
            mode = tf.estimator.ModeKeys.TRAIN
        df = self.get_raw_input(mode)
        dataset = tf.data.Dataset.from_tensor_slices(dict(df))
        #dataset = self.input_fn(mode)
        dataset = dataset.map(lambda value: self.parse_record(mode, value),
                            num_parallel_calls=self.num_parallel_calls)
        return dataset

    def input_fn(self, mode, num_epochs=1):
        #is_training = (mode == tf.estimator.ModeKeys.TRAIN)
        #examples_per_epoch = is_training and NUM_IMAGES['train'] or NUM_IMAGES['validation']
        shuffle_buffer = _SHUFFLE_BUFFER

        df = self.get_raw_input(mode)
        examples_per_epoch = len(df)

        dataset = tf.data.Dataset.from_tensor_slices(dict(df))
        if mode == tf.estimator.ModeKeys.PREDICT:
            dataset = dataset.map(lambda value: self.parse_record(mode, value),
                                num_parallel_calls=self.num_parallel_calls)
        else:
            if mode == tf.estimator.ModeKeys.TRAIN:
                dataset = dataset.shuffle(buffer_size=df.shape[0])

            dataset = self.process(dataset, mode, shuffle_buffer, num_epochs, examples_per_epoch)
        return dataset

    def get_raw_input(self, mode, convert=True):
        if mode == tf.estimator.ModeKeys.TRAIN:
            df = self.train_df
        elif mode == tf.estimator.ModeKeys.EVAL:
            df = self.test_df
        elif mode == tf.estimator.ModeKeys.PREDICT:
            if self.flags.predict_input_file:
                df = pd.DataFrame(data={'image': [self.flags.predict_input_file]})
            else:
                df = self.load_meta_data(mode, convert)

        return df

    def get_metas(self, mode, data_dir):
        """Returns a list of filenames."""
        data_dir = os.path.expanduser(data_dir)
        metas = []
        if mode == tf.estimator.ModeKeys.PREDICT:
            rank_data_dir = os.path.join(data_dir, 'z_rank')
            metas.append((rank_data_dir, os.path.join(rank_data_dir, 'Tests', 'question.csv')))
        else:
            base_data_dir = os.path.join(data_dir, 'base')
            metas.append((base_data_dir, os.path.join(base_data_dir, 'Annotations', 'label.csv')))

            web_data_dir = os.path.join(data_dir, 'web')
            metas.append((web_data_dir, os.path.join(web_data_dir, 'Annotations', 'skirt_length_labels.csv')))
        return metas

    def parse_record(self, mode, record):
        image_buffer = tf.read_file(record['image'])

        if mode == tf.estimator.ModeKeys.PREDICT:
            image = self.preprocess_predict_image(mode, image_buffer)
            if self.flags.debug:
                return image, record['image']
            return image

        image = self.preprocess_image(mode, image_buffer)

        if self.flags.debug:
            return image, record['image'], record['value']

        label = record['value']
        label = tf.one_hot(label, self._num_classes)

        return image, label

    def preprocess_image(self, mode, image_buffer):
        raw_image = tf.image.decode_jpeg(image_buffer, channels=NUM_CHANNELS)
        small_image = pp.image.aspect_preserving_resize(raw_image, _RESIZE_MIN)
        if mode == tf.estimator.ModeKeys.TRAIN:
            crop_image = tf.random_crop(small_image, [HEIGHT, WIDTH, NUM_CHANNELS])
            image = tf.image.random_flip_left_right(crop_image)
        elif mode == tf.estimator.ModeKeys.EVAL:
            image = pp.image.central_crop(small_image, HEIGHT, WIDTH)

        image.set_shape([HEIGHT, WIDTH, NUM_CHANNELS])

        #image = tf.image.per_image_standardization(image)
        image = pp.image.mean_image_subtraction(image, _CHANNEL_MEANS, NUM_CHANNELS)
        if self.flags.debug:
            return raw_image, small_image, image
        return image

    def preprocess_predict_image(self, mode, image_buffer):
        raw_image = tf.image.decode_jpeg(image_buffer, channels=NUM_CHANNELS)
        image = pp.image.aspect_preserving_resize(raw_image, _RESIZE_MIN)

        #image = tf.random_crop(small_image, [HEIGHT, WIDTH, NUM_CHANNELS])
        images = [
            pp.image.central_crop(image, HEIGHT, WIDTH),
            pp.image.top_left_crop(image, HEIGHT, WIDTH),
            pp.image.top_right_crop(image, HEIGHT, WIDTH),
            pp.image.bottom_left_crop(image, HEIGHT, WIDTH),
            pp.image.bottom_right_crop(image, HEIGHT, WIDTH),
        ]
        images += [tf.image.flip_left_right(image) for image in images]
        if self.flags.debug:
            return tuple(images)
        return tf.stack(images)


class FashionAIEstimator(cls.Estimator):
    def __init__(self, flags, train_num_images, num_classes):
        super(FashionAIEstimator, self).__init__(flags, weight_decay=1e-4)

        self.train_num_images = train_num_images
        self.num_classes = num_classes
        self.batch_size = flags.batch_size

    def new_model(self, features, labels, mode, params):
        model = vgg.Model(num_classes=self.num_classes)
        return model

    def optimizer_fn(self, learning_rate):
        optimizer = tf.train.MomentumOptimizer(
                learning_rate=learning_rate,
                momentum=0.9)
        return optimizer

    def learning_rate_fn(self, global_step):
        batch_denom = 256
        boundary_epochs = [30, 60, 80, 90]
        decay_rates = [1, 0.1, 0.01, 0.001, 1e-4]

        initial_learning_rate = 0.1 * self.batch_size / batch_denom
        batches_per_epoch = self.train_num_images / self.batch_size

        # Multiply the learning rate by 0.1 at 100, 150, and 200 epochs.
        boundaries = [int(batches_per_epoch * epoch) for epoch in boundary_epochs]
        vals = [initial_learning_rate * decay for decay in decay_rates]

        global_step = tf.cast(global_step, tf.int32)
        return tf.train.piecewise_constant(global_step, boundaries, vals)

    def model_fn(self, features, labels, mode, params):
        tf.logging.info(features)
        return super(FashionAIEstimator, self).model_fn(features, labels, mode, params)


class FashionAIRunner(cls.Runner):
    def __init__(self, flags, estimator, dataset):
        shape = None
        super(FashionAIRunner, self).__init__(flags, estimator, dataset, shape)

    def run(self):
        if self._run_display():
            return
        if self._run_debug():
            tf.logging.info('run debug finish')
            return
        output = super(FashionAIRunner, self).run()
        if self.flags.predict:
            self.process_predict_output(output)

    def process_predict_output(self, output):
        df = self.dataset.get_raw_input(tf.estimator.ModeKeys.PREDICT, convert=False)
        tf.logging.info('total count: %d', len(df))
        writer = None
        if self.flags.predict_output_dir:
            filename = os.path.join(self.flags.predict_output_dir, 'output.csv')
            writer = open(filename, 'w')
        for i, v in enumerate(output):
            r = df.iloc[i]
            prob = np.mean(v['probabilities'], axis=0)
            pred_label = np.argmax(prob)
            prob_str = ';'.join(np.char.mod('%.4f', prob))
            r['value'] = prob_str
            tf.logging.info('[%d] image: %s, prob: %s, pred: %d', i, r['image'], prob_str, pred_label)
            if writer:
                writer.write('{},{},{}\n'.format(r['image'], r['key'], r['value']))
                writer.flush()
        if writer:
            writer.close()
        tf.logging.info('write to file done!')

    def _run_display(self):
        if not self.flags.display:
            return False
        web.start_server(proxy=self, static_path=self.flags.data_dir)
        #self.on_dataset_index('predict', True, 0, 9)
        return True

    def get_mode(self, mode):
        if mode == 'train':
            mode = tf.estimator.ModeKeys.TRAIN
        elif mode == 'test':
            mode = tf.estimator.ModeKeys.EVAL
        else:
            mode = tf.estimator.ModeKeys.PREDICT
        return mode

    def on_dataset_transfer(self, mode, index):
        pass

    def on_dataset_data(self, mode, index, method):
        mode = self.get_mode(mode)
        df = self.dataset.get_raw_input(mode)
        row = df.loc[index, :]
        #image = self.dataset.parse_record(mode, row)

        image_buffer = tf.read_file(row['image'])

        raw_image = tf.image.decode_jpeg(image_buffer, channels=NUM_CHANNELS)
        image = pp.image.aspect_preserving_resize(raw_image, _RESIZE_MIN)
        if method == 'topleft':
            image = pp.image.top_left_crop(image, HEIGHT, WIDTH)
        elif method == 'topright':
            image = pp.image.top_right_crop(image, HEIGHT, WIDTH)
        elif method == 'bottomleft':
            image = pp.image.bottom_left_crop(image, HEIGHT, WIDTH)
        elif method == 'bottomright':
            image = pp.image.bottom_right_crop(image, HEIGHT, WIDTH)
        else:
            image = tf.random_crop(image, [HEIGHT, WIDTH, NUM_CHANNELS])
        image = tf.cast(image, dtype=tf.uint8)
        image = tf.image.encode_jpeg(image)

        with tf.Session() as sess:
            return sess.run(image)

    @gen.coroutine
    def on_dataset_index(self, mode, predict, page, size):
        mode = self.get_mode(mode)
        df = self.dataset.get_raw_input(mode)

        from_index = page * size
        to_index = (page + 1) * size
        df = df[from_index:to_index]
        if predict:
            def input_fn(mode, df=df, parser=self.dataset, num_parallel_calls=self.flags.num_parallel_calls):
                dataset = tf.data.Dataset.from_tensor_slices(dict(df))
                dataset = dataset.map(lambda value: parser.parse_record(mode, value),
                                    num_parallel_calls=num_parallel_calls)
                return dataset
            self.input_function = input_fn
            self.flags.predict = True
            output = super(FashionAIRunner, self).run()

        items = []
        i = 0
        for index, row in df.iterrows():
            item = {
                'id': index,
                'title': row['image'].rsplit('/', 1)[1],
                'image': '/img' + row['image'],
                'attr': {
                    'class': row['key'],
                    'label': row['value']
                }
            }
            if predict:
                v = next(output)
                prob = np.mean(v['probabilities'], axis=0)
                pred = np.argmax(prob)
                probs = np.char.mod('%.4f', prob)
                #prob_str = ';'.join(np.char.mod('%.4f', prob))
                item['predict'] = {
                    'pred': pred,
                    'probs': probs.tolist(),
                }
            items.append(item)
            i += 1
        #return items
        raise gen.Return(items)

    def _run_debug(self):
        if not self.flags.debug:
            return False

        df = self.dataset.train_df
        print(len(df[df['value'] == 4]))
        print(self.dataset.train_df[:5])
        print(self.dataset.test_df[:5])
        print(self.dataset.train_num_images)
        print(self.dataset.test_num_images)
        print(self.dataset.num_classes)
        return True

        ds = self.dataset.debug_fn()
        data = ds.make_one_shot_iterator().get_next()

        tf.logging.info(data)

        writer = tf.summary.FileWriter('./debug')

        with tf.Session() as sess:
            for step in range(10):
                sops = []
                image_count = len(data[0])
                for i in range(image_count):
                    #name = '{}_{}_{}'.format(i, label, path)
                    name = '{}'.format(i)
                    family = 'step{}'.format(step)
                    image = tf.expand_dims(data[0][i], 0)
                    sop = tf.summary.image(name, image, max_outputs=image_count, family=family)
                    sops.append(sop)
                if len(data) >= 2:
                    sop = tf.summary.text("path", data[1])
                    sops.append(sop)
                if len(data) >= 3:
                    sop = tf.summary.scalar("label", data[2])
                    sops.append(sop)
                summary_op = tf.summary.merge(sops)
                summary = sess.run(summary_op)
                writer.add_summary(summary, step)
                step += 1

        writer.close()
        return True


class FashionAIArgParser(cls.ArgParser):
    def __init__(self):
        super(FashionAIArgParser, self).__init__()
        self.add_argument(
            '--debug', '-dg', action='store_true',
            default=False,
            help='Debug'
        )
        self.add_argument(
            '--attr_key', '-ak',
            default='skirt_length_labels',
            choices=["skirt_length_labels", "neckline_design_labels", "collar_design_labels",
                "sleeve_length_labels", "neck_design_labels", "coat_length_labels",
                "lapel_design_labels", "pant_length_labels"],
            help='[default: %(default)s] Attribute key'
        )
        self.add_argument(
            "--predict_input_file",
            help="Predict input file",
        )
        self.add_argument(
            '--display', action='store_true',
            default=False,
            help='Display'
        )


def main(argv):
    parser = FashionAIArgParser()
    parser.set_defaults(data_dir='~/data/vision/fashionAI',
                        model_dir='./models/vgg/test',
                        train_epochs=30,
                        predict_yield_single=False,
                        pretrain_warm_vars='^((?!dense).)*$')

    flags = parser.parse_args(args=argv[1:])
    tf.logging.info('flags: %s', flags)

    dataset = FashionAIDataSet(flags)

    estimator = FashionAIEstimator(flags,
            train_num_images=dataset.train_num_images,
            num_classes=dataset.num_classes)

    runner = FashionAIRunner(flags, estimator, dataset)
    runner.run()


if __name__ == '__main__':
    tf.logging.set_verbosity(tf.logging.INFO)
    main(argv=sys.argv)
