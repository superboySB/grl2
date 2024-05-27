#!/bin/bash
#!/bin/bash
# Install SC2 and add the custom maps

# Clone the source code.
#git clone git@github.com:tjuHaoXiaotian/pymarl3.git
export PYMARL3_CODE_DIR=$(pwd)

# 1. Install StarCraftII
echo 'Install StarCraftII...'
cd "$HOME"
export SC2PATH="$HOME/StarCraftII"
echo 'SC2PATH is set to '$SC2PATH
if [ ! -d $SC2PATH ]; then
        echo 'StarCraftII is not installed. Installing now ...';
        wget http://blzdistsc2-a.akamaihd.net/Linux/SC2.4.10.zip
        unzip -P iagreetotheeula SC2.4.10.zip
else
        echo 'StarCraftII is already installed.'
fi

# 2. Install the custom maps

# Copy the maps to the target dir.
echo 'Install SMACV1 and SMACV2 maps...'
MAP_DIR="$SC2PATH/Maps/"
if [ ! -d "$MAP_DIR/SMAC_Maps" ]; then
    echo 'MAP_DIR is set to '$MAP_DIR
    if [ ! -d $MAP_DIR ]; then
            mkdir -p $MAP_DIR
    fi
    cp -r "$PYMARL3_CODE_DIR/src/envs/smac_v2/official/maps/SMAC_Maps" $MAP_DIR
else
    echo 'SMACV1 and SMACV2 maps are already installed.'
fi
echo 'StarCraft II and SMAC maps are installed.'

# Install PyTorch and Python Packages
# 3. Install Python dependencies
echo 'Install PyTorch and Python dependencies...'
# conda create -n pymarl python=3.8 -y
# conda activate pymarl

conda install pytorch torchvision torchaudio cudatoolkit=11.1 -c pytorch-lts -c nvidia -y
pip install sacred numpy scipy gym==0.10.8 matplotlib seaborn \
    pyyaml==5.3.1 pygame pytest probscale imageio snakeviz tensorboard-logger

# pip install git+https://github.com/oxwhirl/smac.git
# Do not need install SMAC anymore. We have integrated SMAC-V1 and SMAC-V2 in pymarl3/envs.
pip install "protobuf<3.21"
pip install "pysc2>=3.0.0"
pip install "s2clientprotocol>=4.10.1.75800.0"
pip install "absl-py>=0.1.0"