Here are the scripts to reproduce the results from our paper. To replicate the analyses, please consider the following:

It's easiest to run the scripts on an AWS EC2 instance. For fast replication of the analyses is recommended to set up a large EC2 instance (type r7i.24xlarge, 96vCPUs, 768GB RAM, 300GB SSD storage). Smaller instances can be used as well but may require to adjust the `TileHandlerParallel` n_procs argument. Please follow the instructions about the remote server setup as detailed [here](../README.md#b-cloud-based-setup). Subsequently, run the following sequence of commands:

```
screen                                  # create a screen session (optionally)
source ~/venv/gsemantique/bin/activate  # activate venv
cd ~/repos/gsemantique/paper            # switch to the current subdirectory

bash cloud_stats.sh                     # to replicate Section 5.1 (Application I – cloud-free scenes)
bash composites.sh                      # to replicate Section 5.1 (Application I – cloud-free scenes)
python forest.py                        # to replicate section 5.2 (Application II – forest disturbances)
```