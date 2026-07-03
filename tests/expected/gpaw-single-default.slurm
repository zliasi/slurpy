#!/bin/bash
#SBATCH --job-name=relax
#SBATCH --output=output/%x.log
#SBATCH --nodes=1
#SBATCH --ntasks=8
#SBATCH --cpus-per-task=1
#SBATCH --mem=2gb
#SBATCH --partition=chem
#SBATCH --export=NONE

set -euo pipefail

input_path="relax.py"
stem="relax"
input="$input_path"

mkdir -p output

set +u
export OPENBLAS_NUM_THREADS=1
set -u

mpirun -np "$SLURM_NTASKS" gpaw python -- "$input"

sleep 2
sacct -n -j "$SLURM_JOB_ID" --format=JobID,JobName,MaxRSS,Elapsed,CPUTime --units=MB || true
