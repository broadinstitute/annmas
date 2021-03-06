from io import open

from setuptools import find_packages, setup

# following src dir layout according to
# https://blog.ionelmc.ro/2014/05/25/python-packaging/#the-structure
version = "0.2.0"
setup(
    name="longbow",
    version=version,
    description="Annotation and segmentation of MAS-seq data",
    author="Kiran V Garimella, Jonn Smith",
    author_email="kiran@broadinstitute.org, jonn@broadinstitute.org",
    license="BSD 3-Clause",
    long_description=open("README.md").read(),
    install_requires="""
    """.split(
        "\n"
    ),
    tests_require=["coverage", "pytest"],
    python_requires=">=3.6, <=3.7.9",
    packages=find_packages("src"),
    package_dir={"": "src"},
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Science/Research",
        "License :: OSI Approved :: BSD 3-Clause",
        "Natural Language :: English",
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3.6",
        "Programming Language :: Python :: 3.7",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: Implementation :: CPython",
    ],
    entry_points={"console_scripts": ["longbow=longbow.__main__:main_entry"]},
    include_package_data=True,
)
