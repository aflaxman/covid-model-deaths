#!/bin/bash
# Tell users about usage
if [ "$#" -ne 1 ] || [ "$1" != "prod" ] && [ "$1" != "dev" ]; then
   echo "Usage: $0 prod|dev"
   exit 1
fi

echo "Clearing notebook contents"
for notebook in notebooks/*; do
    if [ -f "${notebook}" ]; then
        echo "clearing contents for ${notebook}"
        jupyter nbconvert --ClearOutputPreprocessor.enabled=True --inplace "${notebook}"
    fi
done

for notebook in notebooks/experiments/*; do
    if [ -f "${notebook}" ]; then
        echo "clearing contents for ${notebook}"
        jupyter nbconvert --ClearOutputPreprocessor.enabled=True --inplace "${notebook}"
    fi
done

echo "Cleaning up and commiting changes."
dt_tag=$(date '+d_%Y_%m_%d_t_%H_%M') &&
branch="${USER}/${1}/${dt_tag}" &&
git checkout -b "$branch" &&
git add . &&
git commit -m "$1 run $dt_tag" &&
echo "Pushing changes to repository" &&
git push --set-upstream origin "$branch" &&
echo "Tagging run" &&

echo "**Done**"
