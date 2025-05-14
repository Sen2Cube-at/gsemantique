#!/bin/bash

# Define the year for which we want to run the script
year=2022

# Define the base name for the output directory
output_dir_base="results/composites"
mkdir -p "$output_dir_base"

# Define the cloud thresholds to test
cloud_thresholds=(100 20)

# Define the log file
log_file="${output_dir_base}/process.log"

# Loop over each cloud threshold
for cloud_thresh in ${cloud_thresholds[@]}
do
    # Define the output directory for this month and cloud threshold
    output_dir="${output_dir_base}"

    # Print the parameters to the console and the log file
    echo "Year: $year, Cloud Threshold: $cloud_thresh" | tee -a "$log_file"

    # Call the Python script with these parameters and log the output
    /home/ubuntu/venv/gsemantique/bin/python3 composites.py \
        --t_start "2022-01-01" \
        --t_end "2023-01-01" \
        --cloud_thresh "$cloud_thresh" \
        --output_dir "$output_dir" 2>&1 | tee -a "$log_file"
done