#!/bin/bash

# define the base name for the output directory
output_dir_base="/home/ubuntu/projects/clouds/results"
mkdir -p "$output_dir_base"

# define the cloud thresholds to test
cloud_thresholds=(100)

# define the log file
log_file="${output_dir_base}/process.log"

# run for each year and month
for year in {2021..2023}
do
    for month in {01..12}
    do
        # define the start and end dates for this month
        t_start="${year}-${month}-01"
        if [ "$month" -eq 12 ]; then
            t_end="$(($year+1))-01-01"
        else
            t_end="${year}-$(printf "%02d" $((10#$month + 1)))-01"
        fi

        # loop over each cloud threshold
        for cloud_thresh in ${cloud_thresholds[@]}
        do
            # define the output directory for this month and cloud threshold
            output_dir="${output_dir_base}"

            # print the parameters to the console and the log file
            echo "Year: $year, Month: $month, Cloud Threshold: $cloud_thresh" | tee -a "$log_file"

            # call the Python script with these parameters and log the output
            /home/ubuntu/venv/gsemantique/bin/python3 large_scale_clouds.py \
                --t_start "$t_start" \
                --t_end "$t_end" \
                --cloud_thresh "$cloud_thresh" \
                --output_dir "$output_dir" 2>&1 | tee -a "$log_file"
        done
    done
done