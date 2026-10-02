# Reproducibility runtime wrapper for the downstream CFD workflow.
#
# This image intentionally starts from the published runtime image that already
# contains OpenFOAM 2412 and scalarTransportFoamTurbulent. The solver source is
# not currently present in this repository; a true from-source rebuild requires
# adding that solver source under cfd/solvers/.
FROM jamesafful/classroom-cfd-worker:hpc-openfoam2412-sct07

LABEL org.opencontainers.image.title="video2cfd OpenFOAM runtime"
LABEL org.opencontainers.image.description="OpenFOAM 2412 runtime for video2cfd downstream CFD workflow"
LABEL org.opencontainers.image.source="https://github.com/baskargroup/video2cfd"

WORKDIR /work
