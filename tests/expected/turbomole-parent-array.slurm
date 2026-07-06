#!/bin/bash
#SBATCH --job-name=benzene
#SBATCH --array=1-2%5
#SBATCH --output=output/%x_%a.log
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=1
#SBATCH --mem=2gb
#SBATCH --partition=chem
#SBATCH --export=NONE

set -euo pipefail

input_path="$(sed -n "${SLURM_ARRAY_TASK_ID}p" ".benzene.manifest")"
stem="$(basename "$(dirname "$input_path")")"
input="$input_path"

mkdir -p output

set +u
export TURBODIR="/opt/turbomole"
export PARA_ARCH=SMP
export PARNODES=1
set -u

cd "$(dirname "$input_path")"
ridft > "$SLURM_SUBMIT_DIR/output/$stem.out" 2>&1

sleep 2
sacct -n -j "$SLURM_JOB_ID" --format=JobID,JobName,MaxRSS,Elapsed,CPUTime --units=MB || true
