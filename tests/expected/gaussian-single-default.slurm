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

input_path="h2o.com"
stem="h2o"

mkdir -p output

scratch="/scratch/$SLURM_JOB_ID"
mkdir -p "$scratch"

set +u
module purge
export GAUSS_SCRDIR="$scratch"
set -u

cp "$input_path" "$scratch/"
cd "$scratch"
input="$(basename "$input_path")"

"/opt/g16/g16" "$input" > "$SLURM_SUBMIT_DIR/output/$stem.out"

for ext in chk; do
  if [[ -f "$stem.$ext" ]]; then
    cp "$stem.$ext" "$SLURM_SUBMIT_DIR/output/"
  fi
done

cd "$SLURM_SUBMIT_DIR"
tar -cJf "output/$stem.tar.xz" -C "$scratch" .
rm -rf "$scratch"

sleep 2
sacct -n -j "$SLURM_JOB_ID" --format=JobID,JobName,MaxRSS,Elapsed,CPUTime --units=MB || true
