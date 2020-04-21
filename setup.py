import setuptools
import re

PACKAGE = "pds-doi-core"

with open("README.md", "r") as fh:
    long_description = fh.read()

with open(f"./{PACKAGE}/__init__.py") as fi:
    result = re.search(r'__version__\s*=\s*[\'"]([^\'"]*)[\'"]', fi.read())
version = result.group(1)


setuptools.setup(
    name=PACKAGE, # Replace with your own username
    version=version,
    license="apache-2.0",
    author="pds ",
    author_email="pds_operator@jpl.nasa.gov",
    description="short description of my pds module, less than 100-120 characters",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/NASA-PDS/pds-template-python",
    download_url = "https://github.com/NASA-PDS/pds-template-python/releases/download/....",
    packages=setuptools.find_packages(),
    keywords=['pds', 'doi', 'osti', 'dataCite'],

    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: Apache Software License",
        "Operating System :: OS Independent",
    ],
    python_requires='>=3.6',
    install_requires=[],
    scripts=['bin/pds-doi-start'],
    entry_points={
        'console_scripts': [],
    },

data_files=[('pds-doi-core',
                 ['config/conf.ini.default']
                 )
            ]


)