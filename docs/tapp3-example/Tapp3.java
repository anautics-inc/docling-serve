/**
 * Tapp3 - Tapp3.java
 * Copyright 2020-2024 by Altair Engineering Inc.
 *
 * This example source code can be freely used and modified completely or
 * in parts by customers of Altair Engineering Inc.
 * It comes with no warranty of any kind and is provided "as-is".
 *
 * Title:
 *      Java Test Application for Altair's Edb Creator API
 *
 * Description:
 *      It creates and loads an Edb and stores it into a file.
 *      Make sure to start the Java VM with -Dfile.encoding=UTF-8
 *      if you are expecting UTF-8 string literals.
 *
 * Arguments:
 *      ?-silent? ?output_filename.edb?
 *
 * @author Lothar Linhard, Ralf Wimmer
 */
package demo;

import de.concept.edb.JedbCreator;
import de.concept.edb.JedbCreator.*;

public class Tapp3 {

    static void exception(Edb edb) throws Exception {
        JedbCreator   f = JedbCreator.INSTANCE;
        throw new Exception(f.EdbLastError(edb));
    }
    // =========================================================================
    // Main
    // =========================================================================
    //
    public static void main(final String args[]) throws Exception
    {
        boolean silent = false;
        boolean ok;
        JedbCreator f = JedbCreator.INSTANCE;

        // Wire colors
        final String red_blue = "#ff0000 #0000ff";
        final String green_orange = "#00aa00 #ffa500";
        final String light_blue = "#8888ff";

        // Abbreviations for component types
        final int inliner = f.EdbComponentTINLINER;
        final int ecu     = f.EdbComponentTECU;
        final int splice  = f.EdbComponentTSPLICE;
        final int eyelet  = f.EdbComponentTEYELET;

        // Create an empty EDB
        Edb edb = f.EdbNew();

        // Create an ECU with one connector and five cavities
        EdbComponent comp = f.EdbNewComponent(edb, "C1", ecu);
        EdbConnector connA = f.EdbNewConnector(edb, comp, "A", 0);
        EdbCavity[] cav = new EdbCavity[4];
        for (int i = 0; i < cav.length; ++i) {
            cav[i] = f.EdbNewCavityEx(edb, connA, Integer.toString(i+1), 0);
        }
        EdbCavity sh1 = f.EdbNewCavityEx(edb, connA, null, f.EdbCavityTHALFDOT);

        // Create an inliner with two connectors (one on each side)
        EdbComponent inl = f.EdbNewComponent(edb, "I1", inliner);
        EdbConnector connB = f.EdbNewConnector(edb, inl, "B",
            f.EdbConnectorTFEMALE);
        EdbConnector connC = f.EdbNewConnector(edb, inl, "C",
            f.EdbConnectorTMALE);
        ok = f.EdbPartnerConnector(edb, connB, connC);
        if (!ok) exception(edb);

        EdbCavity[] cav_left = new EdbCavity[4];
        for (int i = 0; i < cav_left.length; ++i) {
            cav_left[i] = f.EdbNewCavityEx(edb,connB, Integer.toString(i+1),0);
        }
        EdbCavity[] cav_right = new EdbCavity[4];
        for (int i = 0; i < cav_left.length; ++i) {
            cav_right[i] = f.EdbNewCavityEx(edb,connC, Integer.toString(i+1),0);
        }
        EdbCavity sh2 = f.EdbNewCavityEx(edb, connB, null, f.EdbCavityTHALFDOT);

        for (int i = 0; i < cav_left.length; ++i) {
            f.EdbPartnerCavity(edb, cav_left[i], cav_right[i]);
        }

        // Connect the ECU with the left side of the inliner
        EdbWire[] wires_left = new EdbWire[cav.length];
        for (int i = 0; i < cav.length; ++i) {
            wires_left[i] = f.EdbNewWire(edb, "W" + (i+1));
            f.EdbJoin(edb, cav[i], wires_left[i]);
            f.EdbJoin(edb, cav_left[i], wires_left[i]);
        }

        // Connect the shield cavities
        EdbWire sh_wire = f.EdbNewWire(edb, "SHL");
        f.EdbJoin(edb, sh1, sh_wire);
        f.EdbJoin(edb, sh2, sh_wire);

        // Create the multicores
        // ... the outer one
        EdbMulticore mc_outer = f.EdbNewMulticore(edb, null,
            "MC_outer", f.EdbMulticoreTSHIELDED);
        f.EdbShieldWire(edb, sh_wire, mc_outer);

        // ... the two inner ones
        EdbMulticore mc_inner1 = f.EdbNewMulticore(edb, mc_outer,
            "MC_inner1", f.EdbMulticoreTTWSHIELDED);
        f.EdbGroupWire(edb, wires_left[0], mc_inner1);
        f.EdbGroupWire(edb, wires_left[1], mc_inner1);

        EdbMulticore mc_inner2 = f.EdbNewMulticore(edb, mc_outer,
            "MC_inner2", f.EdbMulticoreTTWISTED);
        f.EdbGroupWire(edb, wires_left[2], mc_inner2);
        f.EdbGroupWire(edb, wires_left[3], mc_inner2);

        // Create a SPLICE component with three cavities
        EdbComponent sp = f.EdbNewComponent(edb, "S1", splice);
        EdbConnector spC = f.EdbNewConnector(edb, sp, null, 0);
        EdbCavity[] sp_cav = new EdbCavity[3];
        for (int i = 0; i < 3; ++i) {
            sp_cav[i] = f.EdbNewCavityEx(edb, spC, null, 0);
        }

        // Create an EYELET component with three cavities
        EdbComponent eye = f.EdbNewComponent(edb, "E1", eyelet);
        EdbConnector eyeC = f.EdbNewConnector(edb, eye, null, 0);
        EdbCavity[] eye_cav = new EdbCavity[3];
        for (int i = 0; i < 3; ++i) {
            eye_cav[i] = f.EdbNewCavityEx(edb, eyeC, null, 0);
        }
        
        // Create an ECU with an invisible connector and two cavities.
        // Color the ECU in lightblue and assign it the symbol of the
        // operating resource class "P".
        EdbComponent sensor = f.EdbNewComponent(edb, "Sensor", ecu);
        f.EdbNewAttr4Component(edb, sensor, " imagedsp", "P,40,40");
        f.EdbNewAttr4Component(edb, sensor, " color", light_blue);
        EdbConnector sensorC = f.EdbNewConnector(edb, sensor, null,
            f.EdbConnectorTINVISIBLE);
        EdbCavity[] sensor_cavs = {
            f.EdbNewCavityEx(edb, sensorC, "1", 0),
            f.EdbNewCavityEx(edb, sensorC, "2", 0)
        };

        EdbWire[] wires = new EdbWire[6];
        for (int i = 0; i < 6; ++i) {
            wires[i] = f.EdbNewWire(edb, "W" + (i+5));
        }

        // ... assign color markers to the wires
        for (int i = 0; i < 3; ++i) {
            f.EdbNewAttr4Wire(edb, wires[i], " color", red_blue);
        }

        for (int i = 3; i < 6; ++i) {
            f.EdbNewAttr4Wire(edb, wires[i], " color", green_orange);
        }
        f.EdbNewAttr4Wire(edb, wires[3], "cross\u03c6", "2.5mm\u00b2 \u25a2");

        // ... establish the connections
        f.EdbJoin(edb, cav_right[0], wires[0]);
        f.EdbJoin(edb, sp_cav[0], wires[0]);

        f.EdbJoin(edb, cav_right[1], wires[1]);
        f.EdbJoin(edb, sp_cav[1], wires[1]);

        f.EdbJoin(edb, sp_cav[2], wires[2]);
        f.EdbJoin(edb, sensor_cavs[0], wires[2]);

        f.EdbJoin(edb, cav_right[2], wires[3]);
        f.EdbJoin(edb, eye_cav[0], wires[3]);

        f.EdbJoin(edb, cav_right[3], wires[4]);
        f.EdbJoin(edb, eye_cav[1], wires[4]);

        f.EdbJoin(edb, eye_cav[2], wires[5]);
        f.EdbJoin(edb, sensor_cavs[1], wires[5]);

        // Create a FUNCTION module
        EdbModule fct = f.EdbNewModuleEx(edb, "Processing",
            f.EdbModuleTFUNCTION);
        f.EdbAddObject2Module(edb, fct, comp);
        f.EdbAddObject2Module(edb, fct, connA);
        f.EdbAddObject2Module(edb, fct, cav[0]);
        f.EdbAddObject2Module(edb, fct, cav[1]);
        f.EdbAddObject2Module(edb, fct, wires_left[0]);
        f.EdbAddObject2Module(edb, fct, wires_left[1]);
        f.EdbAddObject2Module(edb, fct, inl);
        f.EdbAddObject2Module(edb, fct, connB);
        f.EdbAddObject2Module(edb, fct, connC);
        f.EdbAddObject2Module(edb, fct, cav_left[0]);
        f.EdbAddObject2Module(edb, fct, cav_left[1]);
        f.EdbAddObject2Module(edb, fct, cav_right[0]);
        f.EdbAddObject2Module(edb, fct, cav_right[1]);
        f.EdbAddObject2Module(edb, fct, sp);
        f.EdbAddObject2Module(edb, fct, sp_cav[0]);
        f.EdbAddObject2Module(edb, fct, sp_cav[1]);
        f.EdbAddObject2Module(edb, fct, wires[0]);
        f.EdbAddObject2Module(edb, fct, wires[1]);

        // Save the EDB to the file specified on the command line
        for (String arg: args) {
            if (arg.equals("-silent")) { silent = true; continue; }
            ok = f.EdbSaveFile(edb, arg);
            if (!ok) exception(edb);
            if (!silent) {
                System.out.println("EDB saved to: "+arg);
            }
            break;
        }
        if (!silent) {
            System.out.println("EDB Version : "+f.EdbVersion());
        }
        f.EdbDelete(edb);
    }
};
