import os
from pathlib import Path
from random import shuffle
import numpy as np
from skimage.data import imread
from skimage.transform import resize
from skimage.io import imsave
import tensorflow as tf

from tools.display import pwc
from tools.file import check_make_dir
from tools.graph import grid_placed
from tools.utils import squarest_grid_size


def read_image(image_path, image_shape=None, preserve_range=True):
  image = imread(image_path)
  if image_shape:
    image = resize(image, image_shape, preserve_range=preserve_range)
  image = image[None]

  return image

def norm_image(image, norm_range=[0, 1]):
  image = tf.cast(image, tf.float32)
  if norm_range == [0, 1]:
    return image / 255.0
  elif norm_range == [-1, 1]:
    return image / 127.5 - 1.
  else:
    raise NotImplementedError

def upsample(x):
  h, w = x.get_shape().as_list()[1:-1]
  x = tf.image.resize_nearest_neighbor(x, [2 * h, 2 * w])
  return x

def save_image(images, path, size=None):
  assert images.shape.ndims == 4, f'images should be 4D, but get shape {images.shape}'
  num_images = images.shape[0]
  if size is None:
    size = squarest_grid_size(num_images)
  images = grid_placed(images, size)
  check_make_dir(path)
  imsave(path, images)

def image_dataset(ds_dir, batch_size, image_size=None, norm_range=None, shuffle=True):
  def preprocess_image(image):
    image = tf.image.decode_jpeg(image, channels=3)
    if image_size:
      image = tf.image.resize(image, image_size)
    if norm_range:
      image = norm_image(image, norm_range)
    return image

  def load_and_preprocess_image(path):
    image = tf.read_file(path)
    return preprocess_image(image)

  if isinstance(ds_dir, list):
    all_image_paths = ds_dir
  else:
    ds_dir = Path(ds_dir)
    assert ds_dir.is_dir(), f'Not a valid directory {ds_dir}'
    all_image_paths = [str(f) for f in Path(ds_dir).glob('*')]
  pwc(f'Total Images: {len(all_image_paths)}', color='magenta')
  ds = tf.data.Dataset.from_tensor_slices(all_image_paths)
  if shuffle:
    ds = ds.shuffle(buffer_size = len(all_image_paths))
  ds = ds.map(load_and_preprocess_image, num_parallel_calls=tf.data.experimental.AUTOTUNE)
  ds = ds.repeat()
  ds = ds.batch(batch_size)
  ds = ds.prefetch(tf.data.experimental.AUTOTUNE)
  image = ds.make_one_shot_iterator().get_next('images')

  return ds, image

class ImageGenerator:
  def __init__(self, ds_dir, image_shape, batch_size, preserve_range=True):
    self.all_image_paths = [str(f) for f in Path(ds_dir).glob('*')]
    pwc(f'Total Images: {len(self.all_image_paths)}', color='magenta')
    self.total_images = len(self.all_image_paths)
    self.image_shape = image_shape
    self.batch_size = batch_size
    self.preserve_range = preserve_range
    self.idx = 0

  def __call__(self):
    while True:
      yield self.sample()
  
  def sample(self):
    if self.idx == 0:
      shuffle(self.all_image_paths)
    
    batch_path = self.all_image_paths[self.idx: self.idx + self.batch_size]
    batch_image = [imread(path) for path in batch_path]
    batch_image = np.array([resize(img, self.image_shape, preserve_range=self.preserve_range) for img in batch_image], dtype=np.float32)
    self.idx += self.batch_size
    if self.idx >= self.total_images:
      self.idx = 0

    return batch_image
