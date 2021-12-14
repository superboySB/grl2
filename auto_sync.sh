#!/bin/zsh

SRC=.
DST1=ubuntu@36.111.131.39:~/grl
DST2=ubuntu@36.111.131.41:~/grl
# DST3=ubuntu@172.16.0.114:~/grl

while true;
do
    rsync -avz --exclude logs* outs $SRC $DST1
    rsync -avz --exclude logs* outs $SRC $DST2
    # rsync -avz --exclude logs* outs $SRC $DST3
    # rsync -avz --exclude logs outs -e 'ssh -p 44139' $SRC $DST3
    sleep 3s
done
