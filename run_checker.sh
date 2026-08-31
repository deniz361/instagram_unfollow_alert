#!/bin/bash

cd /root/instagram_unfollow_alert
source /root/miniconda3/etc/profile.d/conda.sh
conda activate api


xvfb-run -a python3 main.py