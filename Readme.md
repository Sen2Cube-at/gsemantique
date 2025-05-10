# On-demand semantic EO data cubes 

[![Project Status: WIP – Initial development is in progress, but there has not yet been a stable, usable release suitable for the public.](https://www.repostatus.org/badges/latest/wip.svg)](https://www.repostatus.org/#wip)

## Package description
<img src="docs/py_logo.png" align="right" width="150" />

This package builds on top of [semantique](https://zgis.github.io/semantique/#) and extends its functionality by offering:
1. Pre-configured access to a variety of EO datasets with global coverage (e.g. Sentinel-1, Sentinel-2, Landsat)
2. Retrieval & storage mechanisms for the data to persist data cube inputs locally and simplify the rebuilding of the data cubes and replication of the analyses
3. Scaling mechanisms that allow to evaluate recipes for large spatio-temporal extents up to the mesoscale, with internal automatic handling of the required chunking of the processing into smaller parts

## Installation
### A. Local setup 
It is strongly recommended to create a virtual environment before installing the package. The package installation itself can be done in several ways:

1) Using pip to install directly from GitHub:

```
pip install git+https://github.com/Sen2Cube-at/gsemantique.git
```

2) Cloning the repository first and then install with pip:

```
git clone https://github.com/Sen2Cube-at/gsemantique.git
cd gsemantique
pip install .
```

### B. Cloud-based setup
Gsemantique can be deployed on any cloud infrastructure. The following steps describe how to do so within an AWS EC2 computing environment. To setup an AWS EC2 instance with gsemantique, it is necessary to...

1) Launch an EC2 instance as described [here](https://docs.aws.amazon.com/codedeploy/latest/userguide/instances-ec2-create.html#instances-ec2-create-console)
    * OS: choose an Ubuntu image
    * instance type: choose an r-instance (high RAM per CPU ratio) with the desired amount of RAM/CPUs 
    * memory configuration: depending on the size of results to be saved (minimum of 10GB recommended)

2) Access the remote EC2 server
    * download the private key certificate .pem & change permissions to 400 (on Windows shift file to WLS home directory first, then change permissions there)
    * run the following CLI command to access the server: ssh -i "xxx.pem" ubuntu@server_adress.amazonaws.com

3) Configure the server environment

    ```
    # 3.1 Update apt & install core tools
    sudo apt update && sudo apt upgrade -y
    sudo apt install -y git python3 python3-venv python3-pip python3-dev libpq-dev

    # 3.2 Create virtual environment
    mkdir -p venv
    python3 -m venv venv/gsemantique
    source venv/gsemantique/bin/activate

    # 3.3 Install gsemantique
    mkdir repos
    cd repos
    git clone https://github.com/Sen2Cube-at/gsemantique.git
    cd gsemantique
    pip install .
    ```

## Usage
[![Binder](https://mybinder.org/badge_logo.svg)](https://mybinder.org/v2/gh/Sen2Cube-at/gsemantique/main)
[![Google Colab](https://colab.research.google.com/assets/colab-badge.svg)](http://colab.research.google.com/github/Sen2Cube-at/gsemantique/blob/main)  

The package contains several [Jupyter demo notebooks](./demo/). They are providing an overview on the package's operating principles and some hands-on examples. If you want to avoid the installation setup effort and explore the notebooks in an interactive manner, you can make use of Binder or Google Colab. Simply click on the Binder or Google Colab badge. Doing so will setup an online environment for you with the package and all its dependencies installed. No installations or complicated system configurations required, you only need a web browser.

> Note that gsemantique is designed to enable mesoscale analysis of big EO data on common consumer hardware infrastructure. Still, there is a minimum of resources required when working with EO data at scale. Binder and Colab are offering very restricted resources [1, 2]. Therefore it is strongly recommended, to install the package locally and explore it this way.
> 
> [1] Binder sets up a JupyterLab env with 2 CPUs and a guaranteed RAM of at least 1GB (maximum of 4GB).  
> [2] For Colab, the default CPU is an Intel Xeon CPU with 2 vCPUs (virtual CPUs) with about 12GB of RAM. 

A pdf-presentation giving a general overview and introduction to this package and the underlying semantique package can be found [here](docs/intro_slides.pdf).

## Contribution

Contributions of any kind are very welcome! Please see the [contributing guidelines](CONTRIBUTING.md).

## Acknowledgements

The development of this package has been supported by the European Union’s Horizon Europe research and innovation program through the LEONSEGS project ("Large Earth Observation New Space Ecosystem Ground Segment", grant agreement no. 101082493).