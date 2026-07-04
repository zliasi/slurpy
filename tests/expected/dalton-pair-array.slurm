#!/bin/bash
#SBATCH --job-name=dft-a
#SBATCH --array=1-3%5
#SBATCH --output=output/%x_%a.log
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=1
#SBATCH --mem=32gb
#SBATCH --partition=chem
#SBATCH --export=NONE

set -euo pipefail

IFS=$'\t' read -r input_path secondary_path <<< "$(sed -n "${SLURM_ARRAY_TASK_ID}p" ".dft-a.manifest")"
stem="$(basename "$input_path")"
secondary_stem="$(basename "$secondary_path")"
stem="${stem%.*}-${secondary_stem%.*}"

mkdir -p output

scratch="/scratch/$SLURM_JOB_ID/$SLURM_ARRAY_TASK_ID"
mkdir -p "$scratch"

set +u
module purge
dalton_bin="/opt/dalton/dalton32"
if [ 32 -gt 16 ]; then
  dalton_bin="/opt/dalton/dalton64"
fi
export DALTON_TMPDIR="$scratch"
set -u

cp "$input_path" "$scratch/"
cp "$secondary_path" "$scratch/"
cd "$scratch"
input="$(basename "$input_path")"
secondary="$(basename "$secondary_path")"

"$dalton_bin" -d -np 1 -gb 32 -t "$scratch" -dal "$input" -mol "$secondary" -o "$SLURM_SUBMIT_DIR/output/$stem.out"

cd "$SLURM_SUBMIT_DIR"
tar -cJf "output/$stem.tar.xz" -C "$scratch" .
rm -rf "$scratch"

sleep 2
sacct -n -j "$SLURM_JOB_ID" --format=JobID,JobName,MaxRSS,Elapsed,CPUTime --units=MB || true
