#!/bin/bash
#SBATCH --job-name=h2o
#SBATCH --output=output/%x.log
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=1
#SBATCH --mem=2gb
#SBATCH --partition=chem
#SBATCH --export=NONE

set -euo pipefail

input_path="h2o.inp"
stem="h2o"

mkdir -p output

scratch="/scratch/$SLURM_JOB_ID"
mkdir -p "$scratch"

set +u
module purge
export PATH="/opt/orca:$PATH"
export OMP_NUM_THREADS=1
set -u

cp "$input_path" "$scratch/"
cd "$scratch"
input="$(basename "$input_path")"

"/opt/orca/orca" "$input" > "$SLURM_SUBMIT_DIR/output/$stem.out"

for ext in gbw xyz; do
  if [[ -f "$stem.$ext" ]]; then
    cp "$stem.$ext" "$SLURM_SUBMIT_DIR/output/"
  fi
done

cd "$SLURM_SUBMIT_DIR"
rm -rf "$scratch"

sleep 2
sacct -n -j "$SLURM_JOB_ID" --format=JobID,JobName,MaxRSS,Elapsed,CPUTime --units=MB || true
