#!/bin/bash
#SBATCH --job-name=ccsd
#SBATCH --output=output/%x.log
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=2
#SBATCH --mem=2gb
#SBATCH --partition=chem
#SBATCH --export=NONE

set -euo pipefail

input_path="ccsd.inp"
stem="ccsd"

mkdir -p output

scratch="/scratch/$SLURM_JOB_ID"
mkdir -p "$scratch"

set +u
export CFOUR="/opt/cfour"
export OMP_NUM_THREADS=2
set -u

cp "$input_path" "$scratch/"
cd "$scratch"
input="$(basename "$input_path")"

cp "/opt/cfour"/bin/* .
cp "/opt/cfour/basis/GENBAS" GENBAS
cp "$input" ZMAT
./xcfour ./ZMAT ./GENBAS > "$SLURM_SUBMIT_DIR/output/$stem.out"

cd "$SLURM_SUBMIT_DIR"
tar -cJf "output/$stem.tar.xz" -C "$scratch" .
rm -rf "$scratch"

sleep 2
sacct -n -j "$SLURM_JOB_ID" --format=JobID,JobName,MaxRSS,Elapsed,CPUTime --units=MB || true
