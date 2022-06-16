import argparse
import os, sys
import re
import json
from pathlib import Path
import pandas as pd

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from core.typing import ModelPath
from utility.file import search_for_dirs
from utility import yaml_op
from utility.utils import flatten_dict


def get_model_path(dirpath) -> ModelPath:
    d = dirpath.split('/')
    model_path = ModelPath('/'.join(d[:3]), '/'.join(d[3:]))
    return model_path

def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('directory',
                        type=str,
                        default='.')
    parser.add_argument('--prefix', '-p', 
                        type=str)
    parser.add_argument('--target', '-t', 
                        type=str,
                        default='html-logs')
    args = parser.parse_args()

    return args


if __name__ == '__main__':
    args = parse_args()
    
    dirs = search_for_dirs(args.directory, args.prefix, is_suffix=False)
    for d in dirs:
        # if d.split('/')[-1].startswith('0602'):
        #     print(d.split('/')[-1])
        #     continue
        target_dir = '/'.join([args.target, d])
        print(f'copy from {d} to {target_dir}')
        if not os.path.isdir(target_dir):
            Path(target_dir).mkdir(parents=True)
        
        # define paths
        yaml_path = '/'.join([d, 'config_p0.yaml'])
        json_path = '/'.join([target_dir, 'parameter.json'])
        record_path = '/'.join([d, 'nash_conv.txt'])
        process_path = '/'.join([target_dir, 'progress.csv'])
        print('yaml path', yaml_path)
        print('record path', record_path)
        if not os.path.exists(yaml_path) or not os.path.exists(record_path):
            continue
            
        # save config
        config = yaml_op.load_config(yaml_path)
        for k, v in config.items():
            if isinstance(v, dict):
                del v['root_dir']
                del v['model_name']

        config = flatten_dict(config)
        env_names = [(k, v) for k, v in config.items() if k.endswith('env_name')]
        for k, v in env_names:
            prefix = k.rsplit('/', 1)[0]
            suite = v.split('-', 1)[0]
            env_name = v.split('-', 1)[1]
            config[f'{prefix}/suite'] = suite
            config[k] = env_name
        with open(json_path, 'w') as json_file:
            json.dump(config, json_file)

        # save stats
        try:
            data = pd.read_table(record_path, on_bad_lines='skip')
        except:
            print(f'Record path ({record_path}) constains no data')
        if len(data.keys()) == 1:
            data = pd.read_csv(record_path)

        data.to_csv(process_path)
