#!/bin/bash
echo "start youtube_automanager @ $(pwd)"
python -V
python -m "youtube_automanager.runners.automanage"
echo "done youtube_automanager"
