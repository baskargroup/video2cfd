#!/bin/bash
# Tests for cfd/templates/Allrun with stand-in OpenFOAM tools (no OpenFOAM needed).
#
#     bash cfd/tests/test_allrun.sh
#
# The stand-in tools only write what Allrun inspects: time directories, the
# boundary file, processor directories and the probe output. The scenarios
# cover the serial and the parallel path, the field transfer, a scalar run that
# is interrupted while writing a time directory, and a failing tool.
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE="$HERE/../templates/Allrun"
[ -f "$TEMPLATE" ] || { echo "Allrun template not found: $TEMPLATE" >&2; exit 2; }

WORK="$(mktemp -d "${TMPDIR:-/tmp}/video2cfd_allrun_test.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
STUBS="$WORK/stubs"
mkdir -p "$STUBS"

# ----------------------------------------------------------------------------
# Stand-in tools
# ----------------------------------------------------------------------------
cat > "$STUBS/_common.sh" <<'EOF'
latest_numeric() { ls -1 "$1" 2>/dev/null | grep -E '^[0-9]+(\.[0-9]+)?$' | sort -g | tail -1; }
roots() { case " $* " in *" -parallel "*) ls -d processor[0-9]* 2>/dev/null ;; *) echo . ;; esac; }
maybe_fail() { if [ "${FAIL_TOOL:-}" = "$1" ]; then echo "--> FOAM FATAL ERROR: stand-in failure in $1"; exit 1; fi; }
EOF

for tool in blockMesh surfaceFeatureExtract snappyHexMesh topoSet checkMesh; do
cat > "$STUBS/$tool" <<EOF
#!/bin/bash
. "\$(dirname "\$0")/_common.sh"
echo "stand-in $tool \$*"
maybe_fail $tool
[ "$tool" = blockMesh ] && mkdir -p constant/polyMesh
[ "$tool" = checkMesh ] && { echo "    cells:            123456"; echo "Mesh OK."; }
exit 0
EOF
done

cat > "$STUBS/createPatch" <<'EOF'
#!/bin/bash
. "$(dirname "$0")/_common.sh"
echo "stand-in createPatch $*"
maybe_fail createPatch
mkdir -p constant/polyMesh
{
    echo "4"; echo "("
    for p in "room wall 53211" "furniture wall 0" "inlet patch ${INLET_FACES:-1024}" "outlet patch 1152"; do
        set -- $p
        printf '    %s\n    {\n        type            %s;\n        nFaces          %s;\n        startFace       0;\n    }\n' "$1" "$2" "$3"
    done
    echo ")"
} > constant/polyMesh/boundary
exit 0
EOF

cat > "$STUBS/foamDictionary" <<'EOF'
#!/bin/bash
echo "stand-in foamDictionary $*"
# -entry numberOfSubdomains -set N system/decomposeParDict
if [ "$1" = "-entry" ] && [ "$3" = "-set" ]; then
    printf 'numberOfSubdomains %s;\nmethod scotch;\n' "$4" > "$5"
fi
exit 0
EOF

cat > "$STUBS/decomposePar" <<'EOF'
#!/bin/bash
echo "stand-in decomposePar $*"
n="$(awk '$1 == "numberOfSubdomains" { gsub(/;/, "", $2); print $2 }' system/decomposeParDict)"
rm -rf processor[0-9]*
i=0
while [ "$i" -lt "${n:-2}" ]; do
    mkdir -p "processor$i/0" "processor$i/constant"
    echo "T decomposed for processor $i (uniform 1)" > "processor$i/0/T"
    echo "U" > "processor$i/0/U"
    i=$((i + 1))
done
exit 0
EOF

cat > "$STUBS/mpirun" <<'EOF'
#!/bin/bash
if [ "$1" = "-np" ]; then shift 2; fi
exec "$@"
EOF

cat > "$STUBS/simpleFoam" <<'EOF'
#!/bin/bash
. "$(dirname "$0")/_common.sh"
echo "stand-in simpleFoam $*"
for r in $(roots "$@"); do
    for t in 100 200 300 400 500 600 700 800 900 1000; do
        mkdir -p "$r/$t"; echo "U at $t" > "$r/$t/U"; echo "nut at $t" > "$r/$t/nut"
    done
done
maybe_fail simpleFoam
echo End
exit 0
EOF

cat > "$STUBS/scalarTransportFoamTurbulent" <<'EOF'
#!/bin/bash
. "$(dirname "$0")/_common.sh"
echo "stand-in scalarTransportFoamTurbulent $*"
grep -q "scalarTransportFoamTurbulent" system/controlDict || { echo "--> FOAM FATAL ERROR: the active controlDict is not the scalar one"; exit 1; }
stop="$(awk '$1 == "endTime" { gsub(/;/, "", $2); print $2 }' system/controlDict)"
start=""
for r in $(roots "$@"); do
    latest="$(latest_numeric "$r")"
    [ -f "$r/$latest/T" ] || { echo "--> FOAM FATAL ERROR: cannot find file $r/$latest/T"; exit 1; }
    start="$latest"
    if [ -n "${KILL_WHILE_WRITING:-}" ]; then
        # the job is killed after U and nut of this time were written, before T
        mkdir -p "$r/$KILL_WHILE_WRITING"
        echo "U" > "$r/$KILL_WHILE_WRITING/U"; echo "nut" > "$r/$KILL_WHILE_WRITING/nut"
    else
        mkdir -p "$r/$stop"; echo "T solved from $latest to $stop" > "$r/$stop/T"
    fi
done
mkdir -p "postProcessing/monitors/$start"
printf '# Probe 0 (2.85 3.6 1.1)\n#       Time             0\n%s.05 1\n%s 0.4\n' "$start" "$stop" > "postProcessing/monitors/$start/T"
if [ -n "${KILL_WHILE_WRITING:-}" ]; then echo "killed by the scheduler (stand-in)"; exit 137; fi
echo End
exit 0
EOF

cat > "$STUBS/reconstructPar" <<'EOF'
#!/bin/bash
. "$(dirname "$0")/_common.sh"
echo "stand-in reconstructPar $*"
latest="$(latest_numeric processor0)"
mkdir -p "$latest"; echo "T reconstructed at $latest" > "$latest/T"
exit 0
EOF

chmod +x "$STUBS"/*
export PATH="$STUBS:$PATH"
export WM_PROJECT_VERSION=v2412

# ----------------------------------------------------------------------------
# Harness
# ----------------------------------------------------------------------------
pass=0
fail=0
ok()  { echo "  PASS: $*"; pass=$((pass + 1)); }
bad() { echo "  FAIL: $*"; fail=$((fail + 1)); }
expect() { local desc="$1"; shift; if "$@"; then ok "$desc"; else bad "$desc"; fi; }

# newcase <name>: a minimal generated case (only the files Allrun looks at).
newcase() {
    local d="$WORK/$1"
    rm -rf "$d"; mkdir -p "$d/system" "$d/0" "$d/constant"
    printf 'application simpleFoam;\nstartFrom startTime;\nstartTime 0;\nendTime         1000;\n' > "$d/system/controlDict.flow"
    cp "$d/system/controlDict.flow" "$d/system/controlDict"
    printf 'application scalarTransportFoamTurbulent;\nstartFrom latestTime;\nendTime 2200;\n' > "$d/system/controlDict.scalar"
    printf 'numberOfSubdomains 56;\nmethod scotch;\n' > "$d/system/decomposeParDict"
    echo "T initial uniform 1" > "$d/0/T"
    echo "U" > "$d/0/U"
    cp "$TEMPLATE" "$d/Allrun"
    echo "$d"
}

# run <case> [VAR=value ...]: runs Allrun from another directory, prints its exit code
run() {
    local d="$1"; shift
    ( cd "$WORK" && env "$@" bash "$d/Allrun" ) > "$d/_out.txt" 2>&1
    echo $?
}

echo "== T1 serial run: all stages, field transfer, final hint"
d="$(newcase t1)"; rc="$(run "$d")"
expect "exit 0 (rc=$rc)" [ "$rc" -eq 0 ]
for m in 01_blockMesh 02_surfaceFeatureExtract 03_snappyHexMesh 04_topoSet 05_createPatch 06_checkMesh 07_checkPatches 09_simpleFoam 10_fieldTransfer 11_scalarTransport; do
    expect "status/$m.done" [ -f "$d/status/$m.done" ]
done
expect "1000/T is a copy of 0/T" cmp -s "$d/0/T" "$d/1000/T"
expect "900/T not created (numeric, not lexicographic, latest time)" [ ! -e "$d/900/T" ]
expect "scalar result 2200/T written" [ -f "$d/2200/T" ]
expect "active controlDict is the scalar one" cmp -s "$d/system/controlDict" "$d/system/controlDict.scalar"
expect "hint names 06_extract_probe_csv.py --metrics" grep -q -- "06_extract_probe_csv.py.*--metrics" "$d/_out.txt"
expect "hint states t0 = flow end time" grep -q "t0 = 1000" "$d/_out.txt"
expect "patch faces reported" grep -q "patch inlet: 1024 faces" "$d/_out.txt"

echo "== T2 re-run: everything skipped"
rc="$(run "$d")"
expect "exit 0 (rc=$rc)" [ "$rc" -eq 0 ]
expect "no tool was run again" [ "$(grep -c -F '[run ]' "$d/_out.txt")" -eq 0 ]

echo "== T3 parallel run (NPROCS=2)"
d="$(newcase t3)"; rc="$(run "$d" NPROCS=2)"
expect "exit 0 (rc=$rc)" [ "$rc" -eq 0 ]
expect "numberOfSubdomains set to 2" grep -q "numberOfSubdomains 2;" "$d/system/decomposeParDict"
for i in 0 1; do
    expect "processor$i/1000/T copied from processor$i/0/T" cmp -s "$d/processor$i/0/T" "$d/processor$i/1000/T"
done
expect "solvers run in parallel" grep -q "stand-in simpleFoam -parallel" "$d/log.simpleFoam"
expect "reconstructPar marker and 2200/T" [ -f "$d/status/12_reconstructPar.done" -a -f "$d/2200/T" ]

echo "== T4 scalar job killed while writing time 1500 (U, nut written, T not)"
d="$(newcase t4)"; rc="$(run "$d" NPROCS=2 KILL_WHILE_WRITING=1500)"
expect "first job fails (rc=$rc)" [ "$rc" -ne 0 ]
expect "1500 has no T" [ -d "$d/processor0/1500" -a ! -e "$d/processor0/1500/T" ]
rm -f "$d/status/10_fieldTransfer.done"
rc="$(run "$d" NPROCS=2 STAGES=scalar)"
expect "resume without the transfer marker is refused (rc=$rc)" [ "$rc" -ne 0 ]
expect "message names the incomplete write" grep -q "incomplete write of an interrupted scalar run" "$d/_out.txt"
expect "initial field NOT copied into 1500" [ ! -e "$d/processor0/1500/T" -a ! -e "$d/processor1/1500/T" ]
rc="$(run "$d" NPROCS=2 STAGES=scalar FORCE=1)"
expect "FORCE=1 is refused as well (rc=$rc)" [ "$rc" -ne 0 ]
expect "solver not started" [ ! -f "$d/status/11_scalarTransport.done" ]

echo "== T5 the same directory with T is a valid restart and is left untouched"
for i in 0 1; do echo "T written by the first scalar run at 1500" > "$d/processor$i/1500/T"; done
rc="$(run "$d" NPROCS=2 STAGES=scalar FORCE=1)"
expect "resume succeeds (rc=$rc)" [ "$rc" -eq 0 ]
expect "2 existing files left untouched" grep -q "2 existing file(s) left untouched" "$d/_out.txt"
expect "1500/T unchanged" grep -q "first scalar run" "$d/processor1/1500/T"
expect "solver restarted from 1500" grep -q "T solved from 1500 to 2200" "$d/processor1/2200/T"
expect "second probe folder" [ -f "$d/postProcessing/monitors/1500/T" ]

echo "== T6 createPatch fails: no marker, nothing after it runs"
d="$(newcase t6)"; rc="$(run "$d" FAIL_TOOL=createPatch)"
expect "non-zero exit (rc=$rc)" [ "$rc" -ne 0 ]
expect "no 05 marker" [ ! -f "$d/status/05_createPatch.done" ]
expect "checkMesh and simpleFoam not run" [ ! -f "$d/log.checkMesh" -a ! -f "$d/log.simpleFoam" ]
expect "error names the stage" grep -q "stage 05_createPatch failed" "$d/_out.txt"

echo "== T7 empty inlet patch is detected"
d="$(newcase t7)"; rc="$(run "$d" INLET_FACES=0)"
expect "non-zero exit (rc=$rc)" [ "$rc" -ne 0 ]
expect "message: 0 faces" grep -q "patch 'inlet' has 0 faces" "$d/_out.txt"
expect "simpleFoam not run" [ ! -f "$d/log.simpleFoam" ]

echo "== T8 MESH_ONLY=1 needs no scalar solver"
d="$(newcase t8)"; rc="$(run "$d" MESH_ONLY=1 SCALAR_SOLVER=noSuchSolverXYZ)"
expect "exit 0 (rc=$rc)" [ "$rc" -eq 0 ]
expect "07 marker, no flow" [ -f "$d/status/07_checkPatches.done" -a ! -f "$d/log.simpleFoam" ]

echo
echo "RESULT: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
