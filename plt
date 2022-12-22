 12##!/bin/bash

source activate grl


dir=$1

# IFS='/'

# read -r -a arr <<< "$dir"
# logs=${arr[6]}
# ologs=""
# for i in {1..6}; do
#     if [ -z != $ologs ]; then
#         ologs="${ologs}/${arr[i]}"
#     else
#         ologs="${arr[i]}"
#     fi
#     echo $ologs
# done

# echo $ologs

while true
do
    today=$(date +"%m%d")
    yesterday=$(date -v-1d "+%m%d")
    dby=$(date -v-2d "+%m%d")

    # rm -rf html-logs
    python run/html_plt.py $@ -p a0 model -n $today $yesterday $dby
    sleep 600
done
