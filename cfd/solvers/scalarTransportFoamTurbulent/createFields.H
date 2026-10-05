// Modified from applications/solvers/basic/scalarTransportFoam/createFields.H
// (OpenFOAM v2412, GPL-3.0-or-later) for the video2cfd project, October 2026.
// Modification: the turbulent Schmidt number Sct and the turbulent viscosity
// field nut of the frozen flow solution are read, and the effective scalar
// diffusivity Deff = DT + nut/Sct is built from them. The remaining
// statements are those of the stock file.

Info<< "Reading field T\n" << endl;

volScalarField T
(
    IOobject
    (
        "T",
        runTime.timeName(),
        mesh,
        IOobject::MUST_READ,
        IOobject::AUTO_WRITE
    ),
    mesh
);


Info<< "Reading field U\n" << endl;

volVectorField U
(
    IOobject
    (
        "U",
        runTime.timeName(),
        mesh,
        IOobject::MUST_READ,
        IOobject::AUTO_WRITE
    ),
    mesh
);


Info<< "Reading transportProperties\n" << endl;

IOdictionary transportProperties
(
    IOobject
    (
        "transportProperties",
        runTime.constant(),
        mesh,
        IOobject::MUST_READ_IF_MODIFIED,
        IOobject::NO_WRITE
    )
);


Info<< "Reading diffusivity DT\n" << endl;

dimensionedScalar DT("DT", dimViscosity, transportProperties);


Info<< "Reading turbulent Schmidt number Sct\n" << endl;

dimensionedScalar Sct("Sct", dimless, transportProperties);

if (Sct.value() < SMALL)
{
    FatalIOErrorInFunction(transportProperties)
        << "The turbulent Schmidt number Sct must be positive, but Sct = "
        << Sct.value() << nl
        << exit(FatalIOError);
}


// Turbulent viscosity of the frozen flow solution, as written by the flow
// solver (for example simpleFoam) into the start time directory.
//
// The field is only read and re-written; its boundary conditions are never
// evaluated here, so wall-function patch fields keep the values stored in
// the file. It is written with the other fields (as the stock solver does
// for U and phi) so that an interrupted run can be restarted from any
// written time directory.
Info<< "Reading field nut\n" << endl;

volScalarField nut
(
    IOobject
    (
        "nut",
        runTime.timeName(),
        mesh,
        IOobject::MUST_READ,
        IOobject::AUTO_WRITE
    ),
    mesh
);


// Effective scalar diffusivity. The flow is frozen, so this is computed once.
Info<< "Calculating effective diffusivity Deff = DT + nut/Sct\n" << endl;

volScalarField Deff
(
    IOobject
    (
        "Deff",
        runTime.timeName(),
        mesh,
        IOobject::NO_READ,
        IOobject::NO_WRITE
    ),
    DT + nut/Sct
);

Info<< "    DT   = " << DT.value() << nl
    << "    Sct  = " << Sct.value() << nl
    << "    min/max(nut)  = "
    << min(nut).value() << ", " << max(nut).value() << nl
    << "    min/max(Deff) = "
    << min(Deff).value() << ", " << max(Deff).value() << nl
    << endl;

#include "createPhi.H"

#include "createFvOptions.H"
