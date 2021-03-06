# Name: D03.py
#
# Purpose: Updates D03_CombineTiledPolygons. This script merges, dissolves, and filters the output of D02.
#
# Notes: @ Lines 47-53 - Assuming the results from D02 are shapefiles in a common directory allows for much
# faster processing with basic list comprehension. These commented lines would be used if the results of D02
# were stored in a File Geodatabase.
#
# Author: jeff8977

from ngce.pmdm.d.D_Config import *
import arcpy
import time
import sys
import os


def collect_table_inputs(j_id):

    print('Collecting Inputs From Database Table')

    j_id = int(j_id)

    table = JOB_SOURCE

    values = []
    with arcpy.da.SearchCursor(table, ['WMX_Job_ID', 'Project_Dir']) as cursor:
        for r in cursor:
            if r[0] == j_id:
                values.append(r[1])

    if not values:
        raise Exception('Script Was Unable to Acquire Inputs From Table. Please Check Job ID')
    else:
        return values


def handle_d02_output(d02_output, out_workspace):

    print('Handling D02 Output')

    # Merge Results from D02 Output
    merged_feats = os.path.join(out_workspace, "d03_mc.shp")
    arcpy.Merge_management(
        [os.path.join(d02_output, feat) for feat in os.listdir(d02_output) if feat.endswith('.shp')],
        merged_feats
    )

    # # Merge Results From D02 Output (arcpy.ListFeatureClasses)
    # # Not As Fast As Above Process with List Comprehension & File Directory
    # # Assumes d02_output/out_workspace = File Geodatabase
    # arcpy.env.workspace = d02_output
    # output_fcs = arcpy.ListFeatureClasses('pre_*', "POLYGON", "")
    # merged_feats = os.path.join(out_workspace, "d02_mc")
    # arcpy.Merge_management(output_fcs, merged_feats)

    # Add Dummy Field For Dissolve
    arcpy.AddField_management(merged_feats, "dissolve", "SHORT", 2, "", "", "", "NULLABLE")

    # Dissolve Merge on Dummy Field
    dissolved_feats = os.path.join(out_workspace, "d03_dmc.shp")
    arcpy.Dissolve_management(merged_feats, dissolved_feats, "dissolve", "#", "SINGLE_PART", "DISSOLVE_LINES")

    # Add Field for Acreage
    arcpy.AddField_management(dissolved_feats, "ACRES", "DOUBLE", 12, "", "", "", "NULLABLE")
    arcpy.CalculateField_management(dissolved_feats, 'ACRES', '!shape.area@acres!')

    # Select & Export Polygons > 2 Acres
    arcpy.MakeFeatureLayer_management(dissolved_feats, "lyr")

    # Select the features with acreage > 2
    arcpy.SelectLayerByAttribute_management("lyr", "NEW_SELECTION", '"ACRES" > 2')

    # Copy Selection
    arcpy.CopyFeatures_management("lyr", os.path.join(out_workspace, D03_FINAL))

    # Remove Result of MakeFeatureLayer_management
    arcpy.Delete_management("lyr")


if __name__ == "__main__":

    # Get Script Start Time
    start = time.time()

    try:
        # Collect Job ID from Command Line
        job_id = sys.argv[1]

        # Collect Script Inputs from Table
        inputs = collect_table_inputs(job_id)
        project_dir = inputs[0]

        # Create Directory For Script Results
        out_workspace = os.path.join(project_dir, DERIVED, D03, 'RESULTS')
        os.makedirs(out_workspace)

        # Resolve D02 Tiles into D03 Final Output
        d02_output = os.path.join(project_dir, DERIVED, D02, 'RESULTS')
        handle_d02_output(d02_output, out_workspace)

    except Exception as e:
        print('Exception: ', e)

    print('Program Ran: {0}'.format(time.time() - start))
