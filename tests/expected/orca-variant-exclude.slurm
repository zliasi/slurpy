#!/bin/bash
#SBATCH --job-name=h2o
#SBATCH --output=output/%x.log
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=1
#SBATCH --mem=2gb
#SBATCH --partition=chem
#SBATCH --exclude=node[001-004]
#SBATCH --export=NONE

set -euo pipefail

input_path="h2o.inp"
stem="h2o"

mkdir -p output

scratch="/scratch/$SLURM_JOB_ID"
mkdir -p "$scratch"

cp "$input_path" "$scratch/"
cd "$scratch"
input="$(basename "$input_path")"

"/opt/orca-old/orca" "$input" > "$SLURM_SUBMIT_DIR/output/$stem.out"

cd "$SLURM_SUBMIT_DIR"
tar -cJf "output/$stem.tar.xz" -C "$scratch" .
rm -rf "$scratch"

sleep 2
sacct -n -j "$SLURM_JOB_ID" --format=JobID,JobName,MaxRSS,Elapsed,CPUTime --units=MB || true
