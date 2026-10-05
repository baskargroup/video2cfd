# video2cfd CFD runtime image
#
# What this file is: a thin re-tag of the published runtime image, pinned by
# digest. It compiles nothing. Building it only adds labels and a working
# directory, so that a local tag (video2cfd-openfoam-runtime) refers to exactly
# the image below. The run scripts do not need this build: by default
# cfd/scripts/run_openfoam_docker.sh uses the published image directly, by the
# same digest.
#
# What this file is not: a from-source build recipe. It does not build OpenFOAM
# and it does not compile scalarTransportFoamTurbulent from cfd/solvers/. The
# solver binary inside the base image was built outside this repository. To use
# a solver built from the source in cfd/solvers/scalarTransportFoamTurbulent,
# compile it with wmake in an OpenFOAM v2412 development environment and run
# the case's ./Allrun there (see the README in that folder).
#
# The run scripts rely on two things in the base image:
#   - OpenFOAM v2412 with its environment file at
#     /usr/lib/openfoam/openfoam2412/etc/bashrc
#   - the scalarTransportFoamTurbulent executable on PATH
#
# Base image: jamesafful/classroom-cfd-worker, tag hpc-openfoam2412-sct07,
# referenced by digest because the tag is mutable.
FROM jamesafful/classroom-cfd-worker@sha256:d4518b3a1413e1e7ae6c4ac40625a6fec2a7de2d3b04a9a67522ef3691e4b55c

LABEL org.opencontainers.image.title="video2cfd OpenFOAM runtime"
LABEL org.opencontainers.image.description="Re-tag of jamesafful/classroom-cfd-worker:hpc-openfoam2412-sct07 (OpenFOAM v2412 and a pre-built scalarTransportFoamTurbulent) for the video2cfd CFD workflow"
LABEL org.opencontainers.image.source="https://github.com/baskargroup/video2cfd"
LABEL org.opencontainers.image.base.name="docker.io/jamesafful/classroom-cfd-worker:hpc-openfoam2412-sct07"
LABEL org.opencontainers.image.base.digest="sha256:d4518b3a1413e1e7ae6c4ac40625a6fec2a7de2d3b04a9a67522ef3691e4b55c"

WORKDIR /work
