from setuptools import setup, find_packages

# Get the long description from the README file.
with open("Readme.md", encoding="utf-8") as file:
    long_description = file.read()

# List dependencies.
dependencies = [
    "aiohttp-retry",
    "ipykernel",
    "matplotlib",
    "multiprocess",
    "numpy<2.0.0",
    "pandas>=2.0.0",
    "planetary-computer @ git+https://github.com/fkroeber/planetary-computer-sdk-for-python.git",
    "rioxarray<=0.15.5",
    "semantique @ git+https://github.com/fkroeber/semantique.git@latest",
    "stac-asset==0.4.0",
    "stackstac @ git+https://github.com/fkroeber/stackstac.git@fix_v1",
    "tqdm",
]

# List development dependencies.
dev_dependencies = [
    "pytest",
    "flake8",
    "sphinx",
    "mypy",
    "black",
]


# Setup.
setup(
    name="gsemantique",
    version="0.1.0",
    description="On-demand semantic EO data cubes",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/fkroeber/gsemantique",
    author="Felix Kröber",
    author_email="felix.kroeber@plus.ac.at",
    packages=find_packages(),
    package_data={
        "gsemantique.data": ["*.pkl", "*.json"],
    },
    python_requires=">=3.9",
    install_requires=dependencies,
    extras_require={"dev": dev_dependencies},
)
