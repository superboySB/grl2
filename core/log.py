import os
import inspect
import logging
from datetime import datetime


color2num = dict(
  gray=30,
  red=31,
  green=32,
  yellow=33,
  blue=34,
  magenta=35,
  cyan=36,
  white=37,
  crimson=38
)

def colorize(
  string, 
  color, 
  bold=False, 
  highlight=False
):
  """
  Colorize a string.

  This function was originally written by John Schulman.
  """
  attr = []
  num = color2num[color]
  if highlight: num += 10
  attr.append(str(num))
  if bold: attr.append('1')
  return f'\x1b[{";".join(attr)}m{string}\x1b[0m'

def pwc(
  *args, 
  color='red', 
  bold=False, 
  highlight=False, 
  **kwargs
):
  """
  Print with color
  """
  if isinstance(args, (tuple, list)):
    for s in args:
      print(colorize(s, color, bold, highlight), **kwargs)
  else:
    print(colorize(args, color, bold, highlight), **kwargs)


def pwt(*args, **kwargs):
  print(f'{datetime.now()}:', *args, **kwargs)


def pwtc(*args, color='red', bold=False, highlight=False, **kwargs):
  args = (datetime.now(),) + args
  pwc(*args, color=color, bold=bold, highlight=highlight, **kwargs)


def assert_colorize(cond, err_msg=''):
  assert cond, colorize(err_msg, 'red')


""" Logging operations """
def setup_logging(verbose):
  verbose = getattr(logging, verbose.upper())
  logging.basicConfig(
    level=verbose, 
    format=f'%(asctime)s: %(levelname)s: %(name)s: %(message)s',
    datefmt='%Y-%m-%d:%H:%M:%S',
  )

def get_sys_logger(backtrack=1):
  frame = get_frame(backtrack)
  filename = frame.f_code.co_filename
  filename = filename.replace(f'{os.getcwd()}/', '')
  filename = filename.replace('.py', '')
  filename = filename.replace('/', '.')
  logger = logging.getLogger(filename)
  return logger


def do_logging(
  x, 
  prefix='', 
  logger=None, 
  level='pwt', 
  func_lineno=None, 
  backtrack=2, 
  time=False, 
  color=None, 
  bold=False, 
  highlight=False, 
  **log_kwargs, 
):
  if logger is None:
    logger = get_sys_logger(backtrack)
  frame = get_frame(backtrack)
  if func_lineno is None:
    filename = frame.f_code.co_filename
    filename = filename.rsplit('/', 1)[-1]
    funcname = frame.f_code.co_name
    lineno = frame.f_lineno
    func_lineno = ': '.join([filename, funcname, f'line {lineno}'])
  if prefix == '' or prefix.startswith('\t'):
    func_lineno = func_lineno
  else:
    func_lineno = func_lineno + ': ' + prefix
    prefix = ''
  if time:
    func_lineno = ': '.join([str(datetime.now()), func_lineno])
  log_func = {
    'critical': logger.critical,
    'error': logger.error,
    'warning': logger.warning,
    'info': logger.info,
    'debug': logger.debug,
    'print': print,
    'pwt': pwt, 
    'pwc': pwc, 
    'pwtc': pwtc
  }[level.lower()]

  def decorate_content(x):
    content = f'{func_lineno}: {x}'
    if color or bold or highlight:
      content = colorize(content, color=color, bold=bold, highlight=highlight)
    return content

  if isinstance(x, dict):
    for k, v in x.items():
      if isinstance(v, dict):
        log_func(decorate_content(f'{prefix}{k}'))
        do_logging(
          v, 
          logger=logger, 
          prefix='\t'+prefix, 
          level=level, 
          func_lineno=func_lineno, 
          color=color, 
          bold=bold, 
          highlight=highlight
        )
      else:
        log_func(decorate_content(f'{prefix}{k}: {v}'))
  elif isinstance(x, (list, tuple)):
    if any([isinstance(v, dict) for v in x]):
      log_func(decorate_content(f'{prefix}{x.__class__.__name__}'))
      for v in x:
        do_logging(
          v, 
          logger=logger, 
          prefix='\t'+prefix, 
          level=level, 
          func_lineno=func_lineno, 
          color=color, 
          bold=bold, 
          highlight=highlight
        )
    else:
      log_func(decorate_content(f'{prefix}{x}'))
  else:
    log_func(decorate_content(f'{prefix}{x}'))

def get_frame(backtrack):
  frame = inspect.currentframe()
  for _ in range(backtrack):
    if frame.f_back:
      frame = frame.f_back
  return frame


def stringify(x, prefix=''):
  lines = []
  if isinstance(x, tuple) and hasattr(x, '_asdict'):
    lines.append(prefix + f'namedtuple.{x.__class__.__name__}')
  if isinstance(x, str):
    lines.append(x)
  elif isinstance(x, (list, tuple)):
    for v in x:
      if isinstance(v, dict):
        lines += stringify(v, prefix='\t'+prefix)
      else:
        lines.append(str(v))
  elif isinstance(x, dict):
    for k, v in x.items():
      if isinstance(v, dict):
        lines.append(f'{prefix}{k}')
        lines += stringify(v, prefix='\t'+prefix)
      else:
        lines.append(f'{prefix}{k}: {v}')
  else:
    lines.append(f'{prefix}{k}: {v}')
  return lines
