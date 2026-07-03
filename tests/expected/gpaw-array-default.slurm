#!/bin/bash
#SBATCH --job-name=a
#SBATCH --array=1-2%5
#SBATCH --output=output/%x_%a.log
#SBATCH --nodes=1
#SBATCH --ntasks=8
#SBATCH --cpus-per-task=1
#SBATCH --mem=2gb
#SBATCH --partition=chem
#SBATCH --export=NONE

set -euo pipefail

input_path="$(sed -n "${SLURM_ARRAY_TASK_ID}p" ".a.manifest")"
stem="$(basename "$input_path")"
stem="${stem%.*}"
input="$input_path"

mkdir -p output

set +u
export OPENBLAS_NUM_THREADS=1
set -u

mpirun -np "$SLURM_NTASKS" gpaw python -- "$input"

sleep 2
sacct -n -j "$SLURM_JOB_ID" --format=JobID,JobName,MaxRSS,Elapsed,CPUTime --units=MB || true
