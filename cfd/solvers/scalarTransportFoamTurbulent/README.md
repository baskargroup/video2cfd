# scalarTransportFoamTurbulent source status

The production workflow used a custom OpenFOAM solver named
`scalarTransportFoamTurbulent`.

Current reproducibility status:

- The solver binary is preserved in the Docker/Apptainer runtime image.
- The solver binary has been smoke-tested in the repository workflow.
- The original solver source files have not yet been recovered.

Target source files still needed for source-level rebuild:

```text
scalarTransportFoamTurbulent.C
createFields.H
Make/files
Make/options

Until those files are recovered, this repository provides operational
reproducibility through the runtime image, not source-level rebuild of the custom
solver.
