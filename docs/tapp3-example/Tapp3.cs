/**
 * Tapp3
 * Copyright 2021-2024 by Altair Engineering Inc.
 *
 * This example source code can be freely used and modified completely or
 * in parts by customers of Altair Engineering Inc.
 * It comes with no warranty of any kind and is provided "as-is".
 *
 * Title:
 *      .NET C# Test Application for Altair's Edb Creator API
 *
 * Description:
 *      It creates and loads an Edb and stores it into a file.
 *
 * Arguments:
 *      ?-silent? ?output_filename.edb?
 *
 * @author Roland Weber
 */
using System;
using CE;

namespace TestNedbCreator {
  class Tapp3 {

    // The main program
    //
    static void Main(string[] args)
    {
        bool silent = false;
        NedbCreator f = new NedbCreator();

        // Wire colors
        const String red_blue     = "#ff0000 #0000ff";
        const String green_orange = "#00aa00 #ffa500";
        const String light_blue   = "#8888ff";

        // Abbreviations for component types
        const ComponentType inliner  = ComponentType.INLINER;
        const ComponentType ecu      = ComponentType.ECU;
        const ComponentType splice   = ComponentType.SPLICE;
        const ComponentType eyelet   = ComponentType.EYELET;
        const ConnectorType invis    = ConnectorType.INVISIBLE;
        const MulticoreType shielded = MulticoreType.SHIELDED;
        const MulticoreType tws      = MulticoreType.TWSHIELDED;
        const MulticoreType tw       = MulticoreType.TWISTED;

        // Create an empty EDB
        Edb edb = f.New();

        // Create an ECU with one connector and five cavities
        EdbComponent comp  = f.NewComponent(edb, "C1", ecu);
        EdbConnector connA = f.NewConnector(edb, comp, "A", 0);
        EdbCavity[]  cav   = new EdbCavity[4];
        for (int i = 0; i < cav.Length; ++i) {
            cav[i] = f.NewCavityEx(edb, connA, (i+1).ToString(), 0);
        }
        EdbCavity sh1 = f.NewCavityEx(edb, connA, null, CavityType.HALFDOT);

        // Create an inliner with two connectors (one on each side)
        EdbComponent inl   = f.NewComponent(edb, "I1", inliner);
        EdbConnector connB = f.NewConnector(edb, inl, "B", ConnectorType.FEMALE);
        EdbConnector connC = f.NewConnector(edb, inl, "C", ConnectorType.MALE);
        f.PartnerConnector(edb, connB, connC);

        EdbCavity[] cav_left = new EdbCavity[4];
        for (int i = 0; i < cav_left.Length; ++i) {
            cav_left[i] = f.NewCavityEx(edb, connB, (i+1).ToString(), 0);
        }
        EdbCavity[] cav_right = new EdbCavity[4];
        for (int i = 0; i < cav_left.Length; ++i) {
            cav_right[i] = f.NewCavityEx(edb, connC, (i+1).ToString(), 0);
        }
        EdbCavity sh2 = f.NewCavityEx(edb, connB, null, CavityType.HALFDOT);

        for (int i = 0; i < cav_left.Length; ++i) {
            f.PartnerCavity(edb, cav_left[i], cav_right[i]);
        }

        // Connect the ECU with the left side of the inliner
        EdbWire[] wires_left = new EdbWire[cav.Length];
        for (int i = 0; i < cav.Length; ++i) {
            wires_left[i] = f.NewWire(edb, "W" + (i+1));
            f.Join(edb, cav[i], wires_left[i]);
            f.Join(edb, cav_left[i], wires_left[i]);
        }

        // Connect the shield cavities
        EdbWire sh_wire = f.NewWire(edb, "SHL");
        f.Join(edb, sh1, sh_wire);
        f.Join(edb, sh2, sh_wire);

        // Create the multicores
        // ... the outer one
        EdbMulticore mc_outer = f.NewMulticore(edb, null, "MC_outer", shielded);
        f.ShieldWire(edb, sh_wire, mc_outer);

        // ... the two inner ones
        EdbMulticore mc_inner1 = f.NewMulticore(edb, mc_outer, "MC_inner1",tws);
        f.GroupWire(edb, wires_left[0], mc_inner1);
        f.GroupWire(edb, wires_left[1], mc_inner1);

        EdbMulticore mc_inner2 = f.NewMulticore(edb, mc_outer, "MC_inner2", tw);
        f.GroupWire(edb, wires_left[2], mc_inner2);
        f.GroupWire(edb, wires_left[3], mc_inner2);

        // Create a SPLICE component with three cavities
        EdbComponent sp  = f.NewComponent(edb, "S1", splice);
        EdbConnector spC = f.NewConnector(edb, sp, null, 0);
        EdbCavity[] sp_cav = new EdbCavity[3];
        for (int i = 0; i < 3; ++i) {
            sp_cav[i] = f.NewCavityEx(edb, spC, null, 0);
        }

        // Create an EYELET component with three cavities
        EdbComponent eye  = f.NewComponent(edb, "E1", eyelet);
        EdbConnector eyeC = f.NewConnector(edb, eye, null, 0);
        EdbCavity[] eye_cav = new EdbCavity[3];
        for (int i = 0; i < 3; ++i) {
            eye_cav[i] = f.NewCavityEx(edb, eyeC, null, 0);
        }
        
        // Create an ECU with an invisible connector and two cavities.
        // Color the ECU in lightblue and assign it the symbol of the
        // operating resource class "P".
        EdbComponent sensor = f.NewComponent(edb, "Sensor", ecu);
        f.NewAttr4Component(edb, sensor, " imagedsp", "P,40,40");
        f.NewAttr4Component(edb, sensor, " color", light_blue);
        EdbConnector sensorC = f.NewConnector(edb, sensor, null, invis);
        EdbCavity[] sensor_cavs = {
            f.NewCavityEx(edb, sensorC, "1", 0),
            f.NewCavityEx(edb, sensorC, "2", 0)
        };

        EdbWire[] wires = new EdbWire[6];
        for (int i = 0; i < 6; ++i) {
            wires[i] = f.NewWire(edb, "W" + (i+5));
        }

        // ... assign color markers to the wires
        for (int i = 0; i < 3; ++i) {
            f.NewAttr4Wire(edb, wires[i], " color", red_blue);
        }

        for (int i = 3; i < 6; ++i) {
            f.NewAttr4Wire(edb, wires[i], " color", green_orange);
        }
        f.NewAttr4Wire(edb, wires[3], "cross\u03c6", "2.5mm\u00b2 \u25a2");

        // ... establish the connections
        f.Join(edb, cav_right[0], wires[0]);
        f.Join(edb,    sp_cav[0], wires[0]);

        f.Join(edb, cav_right[1], wires[1]);
        f.Join(edb,    sp_cav[1], wires[1]);

        f.Join(edb,      sp_cav[2], wires[2]);
        f.Join(edb, sensor_cavs[0], wires[2]);

        f.Join(edb, cav_right[2], wires[3]);
        f.Join(edb,   eye_cav[0], wires[3]);

        f.Join(edb, cav_right[3], wires[4]);
        f.Join(edb,   eye_cav[1], wires[4]);

        f.Join(edb,     eye_cav[2], wires[5]);
        f.Join(edb, sensor_cavs[1], wires[5]);

        // Create a FUNCTION module
        EdbModule fct = f.NewModuleEx(edb, "Processing", ModuleType.FUNCTION);
        f.AddObject2Module(edb, fct, comp);
        f.AddObject2Module(edb, fct, connA);
        f.AddObject2Module(edb, fct, cav[0]);
        f.AddObject2Module(edb, fct, cav[1]);
        f.AddObject2Module(edb, fct, wires_left[0]);
        f.AddObject2Module(edb, fct, wires_left[1]);
        f.AddObject2Module(edb, fct, inl);
        f.AddObject2Module(edb, fct, connB);
        f.AddObject2Module(edb, fct, connC);
        f.AddObject2Module(edb, fct, cav_left[0]);
        f.AddObject2Module(edb, fct, cav_left[1]);
        f.AddObject2Module(edb, fct, cav_right[0]);
        f.AddObject2Module(edb, fct, cav_right[1]);
        f.AddObject2Module(edb, fct, sp);
        f.AddObject2Module(edb, fct, sp_cav[0]);
        f.AddObject2Module(edb, fct, sp_cav[1]);
        f.AddObject2Module(edb, fct, wires[0]);
        f.AddObject2Module(edb, fct, wires[1]);

        // Save the EDB to the file specified on the command line
        foreach (string arg in args)
        {
            if (arg.Equals("-silent")) { silent = true; continue; }

            f.SaveFile(edb, arg);
            if (!silent) {
                Console.WriteLine("EDB Version: " + f.Version() +
                                  " saved to: " + arg);
            }
            break;
        }
        f.Delete(edb);
    }
  }
}
