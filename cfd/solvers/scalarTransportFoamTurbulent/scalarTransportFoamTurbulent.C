/*---------------------------------------------------------------------------*\
  =========                 |
  \\      /  F ield         | OpenFOAM: The Open Source CFD Toolbox
   \\    /   O peration     |
    \\  /    A nd           | www.openfoam.com
     \\/     M anipulation  |
-------------------------------------------------------------------------------
    Copyright (C) 2011-2017 OpenFOAM Foundation
-------------------------------------------------------------------------------
License
    This file is a derivative work of OpenFOAM.

    OpenFOAM is free software: you can redistribute it and/or modify it
    under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    OpenFOAM is distributed in the hope that it will be useful, but WITHOUT
    ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
    FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License
    for more details.

    You should have received a copy of the GNU General Public License
    along with OpenFOAM.  If not, see <http://www.gnu.org/licenses/>.

Modification notice
    This file is modified from
    applications/solvers/basic/scalarTransportFoam/scalarTransportFoam.C
    (OpenFOAM v2412) for the video2cfd project
    (https://github.com/baskargroup/video2cfd), October 2026.

    Modification: the solver is renamed scalarTransportFoamTurbulent and the
    constant diffusion coefficient DT in the Laplacian term is replaced by
    the effective diffusivity Deff = DT + nut/Sct. Deff is built once in
    createFields.H from the turbulent viscosity field nut of a precomputed
    (frozen) flow solution and the turbulent Schmidt number Sct read from
    constant/transportProperties. Everything else follows the stock solver.

    This file is not part of the OpenFOAM distribution and is not approved
    or endorsed by OpenCFD Ltd, the owner of the OPENFOAM trade mark.

Application
    scalarTransportFoamTurbulent

Group
    grpBasicSolvers

Description
    Passive scalar transport equation solver for a frozen (precomputed)
    turbulent flow field, with a gradient-diffusion (eddy-diffusivity)
    closure for the turbulent scalar flux.

    \heading Solver details
    The equation is given by:

    \f[
        \ddt{T} + \div \left(\vec{U} T\right)
      - \div \left( \left(D_T + \frac{\nu_t}{Sc_t}\right) \grad T \right)
        = S_{T}
    \f]

    Where:
    \vartable
        T       | Passive scalar
        D_T     | Molecular diffusion coefficient [m2/s]
        \nu_t   | Turbulent kinematic viscosity of the frozen flow [m2/s]
        Sc_t    | Turbulent Schmidt number [-]
        S_T     | Source
    \endvartable

    \heading Required fields
    \plaintable
        T       | Passive scalar
        U       | Velocity [m/s]
        nut     | Turbulent kinematic viscosity [m2/s]
    \endplaintable

    The face flux field phi is read from the start time directory when it
    is present (as written by the flow solver) and is otherwise interpolated
    from U. The flow fields U, phi and nut are read once and are not updated
    during the run.

    \heading Required entries in constant/transportProperties
    \plaintable
        DT      | Molecular diffusion coefficient [m2/s]
        Sct     | Turbulent Schmidt number [-]
    \endplaintable

\*---------------------------------------------------------------------------*/

#include "fvCFD.H"
#include "fvOptions.H"
#include "simpleControl.H"

// * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * //

int main(int argc, char *argv[])
{
    argList::addNote
    (
        "Passive scalar transport equation solver for a frozen turbulent"
        " flow field (effective diffusivity DT + nut/Sct)."
    );

    #include "addCheckCaseOptions.H"
    #include "setRootCaseLists.H"
    #include "createTime.H"
    #include "createMesh.H"

    simpleControl simple(mesh);

    #include "createFields.H"

    // * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * //

    Info<< "\nCalculating scalar transport\n" << endl;

    #include "CourantNo.H"

    while (simple.loop())
    {
        Info<< "Time = " << runTime.timeName() << nl << endl;

        while (simple.correctNonOrthogonal())
        {
            fvScalarMatrix TEqn
            (
                fvm::ddt(T)
              + fvm::div(phi, T)
              - fvm::laplacian(Deff, T)
             ==
                fvOptions(T)
            );

            TEqn.relax();
            fvOptions.constrain(TEqn);
            TEqn.solve();
            fvOptions.correct(T);
        }

        runTime.write();
    }

    Info<< "End\n" << endl;

    return 0;
}


// ************************************************************************* //
