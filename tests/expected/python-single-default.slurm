#!/bin/bash
#SBATCH --job-name=analysis
#SBATCH --output=output/%x.log
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4
#SBATCH --mem=2gb
#SBATCH --partition=chem
#SBATCH --export=NONE

set -euo pipefail

input_path="analysis.py"
stem="analysis"
input="$input_path"

mkdir -p output

set +u
export OMP_NUM_THREADS=4
set -u

python3 "$input"

sleep 2
sacct -n -j "$SLURM_JOB_ID" --format=JobID,JobName,MaxRSS,Elapsed,CPUTime --units=MB || true
