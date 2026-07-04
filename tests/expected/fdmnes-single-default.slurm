#!/bin/bash
#SBATCH --job-name=CeS8_inp
#SBATCH --output=output/%x.log
#SBATCH --nodes=1
#SBATCH --ntasks=4
#SBATCH --cpus-per-task=1
#SBATCH --mem=2gb
#SBATCH --partition=chem
#SBATCH --export=NONE

set -euo pipefail

input_path="input/CeS8_inp.txt"
stem="CeS8_inp"

mkdir -p output

scratch="/scratch/$SLURM_JOB_ID"
mkdir -p "$scratch"

set +u
. "/opt/fdmnes/mpirt/bin/intel64/mpivars.sh"
set -u

cp "$input_path" "$scratch/"
cd "$scratch"
input="$(basename "$input_path")"

rm -f "$input"
rsync -a --exclude=output --exclude=fdmfile.txt "$SLURM_SUBMIT_DIR/" .
printf '1\n\n%s\n' "$input_path" > fdmfile.txt
"/opt/fdmnes/mpirt/bin/intel64/mpirun" -np 4 "/opt/fdmnes/fdmnes_mpi_linux64" > "$SLURM_SUBMIT_DIR/output/$stem.out"
rsync -a --exclude=fdmfile.txt --exclude=output "$PWD/" "$SLURM_SUBMIT_DIR/output/"

cd "$SLURM_SUBMIT_DIR"
rm -rf "$scratch"

sleep 2
sacct -n -j "$SLURM_JOB_ID" --format=JobID,JobName,MaxRSS,Elapsed,CPUTime --units=MB || true
