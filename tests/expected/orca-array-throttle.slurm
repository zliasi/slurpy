#!/bin/bash
#SBATCH --job-name=a
#SBATCH --array=1-3%2
#SBATCH --output=output/%x_%a.log
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=1
#SBATCH --mem=2gb
#SBATCH --partition=chem
#SBATCH --export=NONE

set -euo pipefail

input_path="$(sed -n "${SLURM_ARRAY_TASK_ID}p" ".a.manifest")"
stem="$(basename "$input_path")"
stem="${stem%.*}"

mkdir -p output

scratch="/scratch/$SLURM_JOB_ID/$SLURM_ARRAY_TASK_ID"
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
tar -cJf "output/$stem.tar.xz" -C "$scratch" .
rm -rf "$scratch"

sleep 2
sacct -n -j "$SLURM_JOB_ID" --format=JobID,JobName,MaxRSS,Elapsed,CPUTime --units=MB || true
