import arcpy
from arcpy.sa import Functions
from datetime import datetime
from functools import partial
from multiprocessing import Pool, cpu_count
import os
import sys
import time

import arcpy.cartography as ca
from ngce import Utility
from ngce.cmdr.CMDR import ProjectJob
from ngce.cmdr.CMDRConfig import OCS
from ngce.cmdr.JobUtil import getProjectFromWMXJobID
from ngce.contour.ContourConfig import CONTOUR_GDB_NAME, WEB_AUX_SPHERE, \
    CONTOUR_INTERVAL, CONTOUR_UNIT, CONTOUR_SMOOTH_UNIT, \
    DISTANCE_TO_CLIP_MOSAIC_DATASET, DISTANCE_TO_CLIP_CONTOURS, SKIP_FACTOR, CONTOUR_NAME_OCS, CONTOUR_NAME_WM
from ngce.folders import ProjectFolders
from ngce.folders.FoldersConfig import DTM
from ngce.pmdm.a import A05_C_ConsolidateRasterInfo
from ngce.pmdm.a.A04_B_CreateLASStats import doTime, deleteFileIfExists
from ngce.pmdm.a.A05_B_RevalueRaster import FIELD_INFO, V_UNIT
from ngce.raster import Raster

CPU_HANDICAP = 1
TRIES_ALLOWED = 10

def generateHighLow(workspace, name, clip_contours, ref_md):
    cont_poly1 = os.path.join(workspace, 'O12_poly_' + name + '.shp')
    cont_poly2 = os.path.join(workspace, 'O13_poly_' + name + '.shp')
    arcpy.FeatureToPolygon_management(in_features=clip_contours, out_feature_class=cont_poly1, cluster_tolerance="", attributes="ATTRIBUTES", label_features="")
    arcpy.MultipartToSinglepart_management(in_features=cont_poly1, out_feature_class=cont_poly2)
    select_set = []
    with arcpy.da.UpdateCursor(cont_poly2, ["FID", "SHAPE@"]) as cursor:  # @UndefinedVariable
        for row in cursor:
            parts = row[1].partCount
            boundaries = row[1].boundary().partCount
            if boundaries > parts:
                select_set.append(row[0])
    
    cont_poly3 = 'O13_poly_' + name + '_layer'
    arcpy.MakeFeatureLayer_management(in_features=cont_poly2, out_layer=cont_poly3, where_clause='"FID" IN(' + ','.join(select_set) + ')', workspace="", field_info="")
    arcpy.DeleteFeatures_management(cont_poly3)
    arcpy.AddSurfaceInformation_3d(in_feature_class=cont_poly2, in_surface=ref_md, out_property="Z_MEAN", method="BILINEAR")

def generate_con_workspace(con_folder):

    # Create File GDB for Contours
    if not os.path.exists(con_folder):
        os.makedirs(con_folder)

    contour_file_gdb_path = os.path.join(con_folder, CONTOUR_GDB_NAME)
    if not os.path.exists(contour_file_gdb_path):
        arcpy.AddMessage("\nCreating Contour GDB:   {0}".format(contour_file_gdb_path))
        arcpy.CreateFileGDB_management(
            con_folder,
            CONTOUR_GDB_NAME,
            out_version="CURRENT"
        )

    # Create Scratch Folder for Intermediate Products
    scratch_path = os.path.join(con_folder, 'C01Scratch')
    arcpy.AddMessage("\nCreating Scratch Folder:    " + scratch_path)
    if not os.path.exists(scratch_path):
        os.makedirs(scratch_path)

    return (contour_file_gdb_path, scratch_path)

def createRefDTMMosaic(in_md_path, out_md_path, v_unit):
    a = datetime.now()
    if arcpy.Exists(out_md_path):
        arcpy.AddMessage("Referenced mosaic dataset exists " + out_md_path)
    else:
        arcpy.CreateReferencedMosaicDataset_management(in_dataset=in_md_path, out_mosaic_dataset=out_md_path, where_clause="TypeID = 1")
        
        raster_function_path = Raster.Contour_Meters_function_chain_path
        v_unit = str(v_unit).upper()
        if v_unit.find("FEET") >= 0 or v_unit.find("FOOT") >= 0 or  v_unit.find("FT") >= 0:
            raster_function_path = Raster.Contour_IntlFeet_function_chain_path
            #if v_unit.find("INTL") >= 0 or v_unit.find("INTERNATIONAL") >= 0 or v_unit.find("STANDARD") >= 0 or v_unit.find("STD") >= 0:
            #    raster_function_path = Raster.Contour_IntlFeet_function_chain_path
            if v_unit.find("US") >= 0 or v_unit.find("SURVEY") >= 0:
                arcpy.AddMessage("Using US FOOT Raster Function")
                raster_function_path = Raster.Contour_Feet_function_chain_path
            else:
                arcpy.AddMessage("Using INT FOOT Raster Function")
        else:
            arcpy.AddMessage("Using METER Raster Function")
                
        arcpy.EditRasterFunction_management(in_mosaic_dataset=out_md_path, edit_mosaic_dataset_item="EDIT_MOSAIC_DATASET", edit_options="REPLACE", function_chain_definition=raster_function_path, location_function_name="")
        Utility.addToolMessages()
        
        arcpy.CalculateStatistics_management(in_raster_dataset=out_md_path, x_skip_factor=SKIP_FACTOR, y_skip_factor=SKIP_FACTOR, ignore_values="", skip_existing="OVERWRITE", area_of_interest="Feature Set")
    
        doTime(a, "Created referenced mosaic dataset " + out_md_path)
    


    

def create_iterable(scratch_folder, prints, distance_to_clip_md, distance_to_clip_contours):
    a = datetime.now()
    arcpy.AddMessage('Create Multiprocessing Iterable')

    ext_dict = {}
    # Go up one directory so we don't have to delete if things go wrong down in scratch
    tmp_scratch_folder = os.path.split(scratch_folder)[0]
    tmp_buff_name = os.path.join(tmp_scratch_folder, "footprints_clip_md.shp")
    if not os.path.exists(tmp_buff_name):
        arcpy.Buffer_analysis(
            prints,
            tmp_buff_name,
            "{} METERS".format(distance_to_clip_md)
        )
        arcpy.AddMessage("Created new {}".format(tmp_buff_name))
    else:
        arcpy.AddMessage("Using existing {}".format(tmp_buff_name))
    
    
    with arcpy.da.SearchCursor(tmp_buff_name, ["Name", "SHAPE@", "zran"]) as cursor:  # @UndefinedVariable

        for row in cursor:

            row_info = []

            # Get Values
            rowname = row[0]
            geom = row[1]
            zran = row[2]
            if zran > 0 and isProcessFile(rowname, scratch_folder):
                box = geom.extent.polygon
    
                row_info.append(box)
                ext_dict[rowname] = row_info
        
    tmp_buff_name2 = os.path.join(tmp_scratch_folder, "footprints_clip_cont.shp")
    if not os.path.exists(tmp_buff_name2):
        arcpy.Buffer_analysis(
            prints,
            tmp_buff_name2,
            "{} METERS".format(distance_to_clip_contours)
        )
        arcpy.AddMessage("Created new {}".format(tmp_buff_name2))
    else:
        arcpy.AddMessage("Using existing {}".format(tmp_buff_name2))
    
    with arcpy.da.SearchCursor(tmp_buff_name2, ["Name", "SHAPE@", "zran"]) as cursor:  # @UndefinedVariable

        for row in cursor:

            # Get Values
            rowname = row[0]
            geom = row[1]
            zran = row[2]
            if zran > 0 and isProcessFile(rowname, scratch_folder):
                row_info = ext_dict[rowname]
                row_info.append(geom)
                ext_dict[rowname] = row_info
    
    for index, item in enumerate(ext_dict.items()):
        row = item[1]
        row.append(index)
        
    
    arcpy.AddMessage('Multiprocessing Tasks: ' + str(len(ext_dict)))
    a = doTime(a, "Created Runnable Dictionary")
    return ext_dict




def generate_contour(md, cont_int, contUnits, rasterUnits, smooth_tol, scratch_path, proc_dict):
    
    name = proc_dict[0]
    index = str(proc_dict[1][2])

    arcpy.AddMessage("Checking out licenses")
    arcpy.CheckOutExtension("3D")
    arcpy.CheckOutExtension("Spatial")


    created = False
    tries = 0
    while not created and tries <= TRIES_ALLOWED:
        tries = tries + 1

        try:
            
            a = datetime.now()
            aa = a
            Utility.setArcpyEnv(True)
            arcpy.AddMessage('STARTING ' + name + ' ' + index + ': Generating Contours')
            
            buff_poly = proc_dict[1][0]
            clip_poly = proc_dict[1][1]
            #arcpy.AddMessage("\t{}: Buffer Poly '{}'".format(name, buff_poly))
            #arcpy.AddMessage("\t{}: Clip Poly '{}'".format(name, clip_poly))
            
            arcpy.env.extent = buff_poly.extent
            
            workspace = os.path.join(scratch_path, name)
            
            if not os.path.exists(workspace):
                # Don't delete if it exists, keep our previous work to save time
                os.mkdir(workspace)
            
            arcpy.env.workspace = workspace
            a = doTime(a, '\t' + name + ' ' + index + ': Created scratch workspace' + workspace)
            
            focal2_path = md
            md_desc = arcpy.Describe(md)
            if not md_desc.referenced:
                arcpy.AddError("\t{}: ERROR Referenced Mosaic not found '{}'".format(name, focal2_path))
    ##            md_layer = arcpy.MakeMosaicLayer_management(in_mosaic_dataset=md, out_mosaic_layer="DTM_MosaicLayer", where_clause="TypeID = 1", template=buff_poly.extent)
    ##            a = doTime(a, "\t" + name + ": Created mosaic layer for primary images")
    ##        
    ##            divide1_name = 'O01_Divide1_' + name + '.tif'
    ##            divide1_path = os.path.join(workspace, divide1_name)
    ##            if not os.path.exists(divide1_path):
    ##                arcpy.MakeRasterLayer_management(in_raster=md_layer, out_rasterlayer=divide1_name)
    ##                # @TODO: Clean up the unit conversion here. output will ALWAYS be US Survey Feet
    ###                 contUnits = contUnits.upper()
    ##                contUnits = "FOOT_US"
    ##                rasterUnits = rasterUnits.upper()
    ##                outDivide1 = Functions.Divide(divide1_name, 1.0)
    ###                 if contUnits.find("METERS") >= 0 or contUnits.find("MT") >= 0:  
    ###                     contUnits = "METER"
    ###                 elif contUnits.find("FOOT") >= 0 or contUnits.find("FEET") >= 0 or contUnits.find("FT") >= 0:
    ###                     contUnits = "FOOT_INTL"
    ###                     if contUnits.find("US") >= 0 or contUnits.find("SURVEY") >= 0:  
    ###                         contUnits = "FOOT_US"
    ###                 
    ####                if rasterUnits.find("METERS") >= 0 or rasterUnits.find("MT") >= 0:  
    ####                    rasterUnits = "METER"
    ####                elif rasterUnits.find("FOOT") >= 0 or rasterUnits.find("FEET") >= 0 or rasterUnits.find("FT") >= 0:
    ####                    rasterUnits = "FOOT_INTL"
    ####                    if rasterUnits.find("US") >= 0 or rasterUnits.find("SURVEY") >= 0:  
    ####                        rasterUnits = "FOOT_US"
    ##                    
    ###                 if contUnits == "METER":
    ###                     if rasterUnits == "METER":
    ###                         outDivide1 = Functions.Divide(divide1_name, 1.0)
    ###                     elif rasterUnits == "FOOT_US":
    ###                         outDivide1 = Functions.Times(divide1_name, 1200.0 / 3937.0)
    ###                     elif rasterUnits == "FOOT_INTL":
    ###                         outDivide1 = Functions.Times(divide1_name, 0.3048)
    ###                 elif contUnits == "FOOT_US":
    ####                if rasterUnits == "METER":
    ####                    outDivide1 = Functions.Times(divide1_name, 1.0 / (1200.0 / 3937.0))
    ####                elif rasterUnits == "FOOT_US":
    ####                    outDivide1 = Functions.Divide(divide1_name, 1.0)
    ####                elif rasterUnits == "FOOT_INTL":
    ####                    outDivide1 = Functions.Times(divide1_name, 0.3048 / (1200.0 / 3937.0))
    ###                 elif contUnits == "FOOT_INTL":
    ###                     if rasterUnits == "METER":
    ###                         outDivide1 = Functions.Times(divide1_name, 1.0 / (0.3048))
    ###                     elif rasterUnits == "FOOT_US":
    ###                         outDivide1 = Functions.Times(divide1_name, (1200.0 / 3937.0) / 0.3048)
    ###                     elif rasterUnits == "FOOT_INTL":
    ###                         outDivide1 = Functions.Divide(divide1_name, 1.0)
    ####                else:
    ####                    arcpy.AddMessage("\ncontourUnits: {}, rasterUnits: {}".format(contUnits, rasterUnits))
    ####                    arcpy.AddError('\nUnable to create contours.')
    ####                    raise Exception("Units not valid")
    ##                
    ##                outDivide1.save(divide1_path)
    ##                del outDivide1
    ##                a = doTime(a, '\t' + name + ' ' + index + ': Converted raster units ' + rasterUnits + ' to ' + contUnits + ' = ' + divide1_path)
    ##            
    ##            focal1_name = 'O02_Focal1_' + name + '.tif'
    ##            focal1_path = os.path.join(workspace, focal1_name)
    ##            if not os.path.exists(focal1_path):
    ##                arcpy.MakeRasterLayer_management(in_raster=divide1_path, out_rasterlayer=focal1_name)
    ##                outFS = Functions.FocalStatistics(in_raster=focal1_name, neighborhood="Rectangle 3 3 CELL", statistics_type="MEAN", ignore_nodata="DATA")
    ##                outFS.save(focal1_path)
    ##                del outFS
    ##                a = doTime(a, '\t' + name + ' ' + index + ': Focal statistics on ' + focal1_path)
    ##            
    ##            times1_name = 'O03_Times_' + name + '.tif'
    ##            times1_path = os.path.join(workspace, times1_name)
    ##            if not os.path.exists(times1_path):
    ##                arcpy.MakeRasterLayer_management(in_raster=focal1_path, out_rasterlayer=times1_name)
    ##                outTimes = Functions.Times(times1_name, 100)
    ##                outTimes.save(times1_path)
    ##                del outTimes
    ##                a = doTime(a, '\t' + name + ' ' + index + ': Times 100 on ' + times1_path)
    ##            
    ##            plus1_name = 'O04_Plus_' + name + '.tif'
    ##            plus1_path = os.path.join(workspace, plus1_name)
    ##            if not os.path.exists(plus1_path):
    ##                arcpy.MakeRasterLayer_management(in_raster=times1_path, out_rasterlayer=plus1_name)
    ##                outPlus = Functions.Plus(plus1_name, 0.5)
    ##                outPlus.save(plus1_path)
    ##                del outPlus
    ##                a = doTime(a, '\t' + name + ' ' + index + ': Plus 0.5 ' + plus1_path)
    ##            
    ##            round1_name = 'O05_Round_' + name + '.tif'
    ##            round1_path = os.path.join(workspace, round1_name)
    ##            if not os.path.exists(round1_path):
    ##                arcpy.MakeRasterLayer_management(in_raster=plus1_path, out_rasterlayer=round1_name)
    ##                outRoundDown = Functions.RoundDown(round1_name)
    ##                outRoundDown.save(round1_path)
    ##                del outRoundDown
    ##                a = doTime(a, '\t' + name + ' ' + index + ': Round Down ' + round1_path)
    ##            
    ##            divide2_name = 'O06_Divide2_' + name + '.tif'
    ##            divide2_path = os.path.join(workspace, divide2_name)
    ##            if not os.path.exists(divide2_path):
    ##                arcpy.MakeRasterLayer_management(in_raster=round1_path, out_rasterlayer=divide2_name)
    ##                outDivide2 = Functions.Divide(divide2_name, 100)
    ##                outDivide2.save(divide2_path)
    ##                del outDivide2
    ##                a = doTime(a, '\t' + name + ' ' + index + ': Divide 100 ' + divide2_path)
    ##                
    ##            focal2_name = 'O07_Focal2_' + name + '.tif'
    ##            focal2_path = os.path.join(workspace, focal2_name)
    ##            if not os.path.exists(focal2_path):
    ##                arcpy.MakeRasterLayer_management(in_raster=divide2_path, out_rasterlayer=focal2_name)
    ##                outFS2 = Functions.FocalStatistics(focal2_name, "Rectangle 3 3 CELL", "MEAN", "DATA")
    ##                outFS2.save(focal2_path)
    ##                del outFS2
    ##                a = doTime(a, '\t' + name + ' ' + index + ': Focal Statistics #2 ' + focal2_path)
                        
        #         a = doTime(a, '\t{}: Calculating statistics {}'.format(raster_name, contour_ready_path))
        #         arcpy.CalculateStatistics_management(in_raster_dataset=contour_ready_path, x_skip_factor="1", y_skip_factor="1", ignore_values="", skip_existing="OVERWRITE", area_of_interest="Feature Set")

            
            
            arcpy.AddMessage("\t{}: Referenced Mosaic found '{}'".format(name, focal2_path))
            base_name = 'O08_BaseCont_' + name + '.shp'
            base_contours = os.path.join(workspace, base_name)
            if not os.path.exists(base_contours):
                arcpy.MakeRasterLayer_management(in_raster=focal2_path, out_rasterlayer=base_name)
                Functions.Contour(
                    base_name,
                    base_contours,
                    int(cont_int)
                )
                a = doTime(a, '\t' + name + ' ' + index + ': Contoured to ' + base_contours)
        
            simple_contours = os.path.join(workspace, 'O09_SimpleCont_' + name + '.shp')
            if not os.path.exists(simple_contours):
                ca.SimplifyLine(
                    base_contours,
                    simple_contours,
                    "POINT_REMOVE",
                    "0.000001 DecimalDegrees",
                    "FLAG_ERRORS",
                    "NO_KEEP",
                    "NO_CHECK"
                )
                a = doTime(a, '\t' + name + ' ' + index + ': Simplified to ' + simple_contours)
            
            smooth_contours = os.path.join(workspace, 'O10_SmoothCont_' + name + '.shp')
            if not os.path.exists(smooth_contours):
                ca.SmoothLine(
                    simple_contours,
                    smooth_contours,
                    "PAEK",
                    "{} DecimalDegrees".format(smooth_tol),
                    "",
                    "NO_CHECK"
                )
                a = doTime(a, '\t' + name + ' ' + index + ': Smoothed to ' + smooth_contours)
            
            # put this up one level to avoid re-processing all of above if something goes wrong below
            clip_workspace = os.path.split(workspace)[0]
            clip_contours = os.path.join(clip_workspace, 'O11_ClipCont_' + name + '.shp')
            if not os.path.exists(clip_contours):
                arcpy.Clip_analysis(
                    in_features=smooth_contours,
                    clip_features=clip_poly,
                    out_feature_class=clip_contours
                )
                a = doTime(a, '\t' + name + ' ' + index + ': Clipped to ' + clip_contours)
            
            arcpy.RepairGeometry_management(in_features=clip_contours,
                                            delete_null="DELETE_NULL")
            
            Utility.addAndCalcFieldLong(dataset_path=clip_contours,
                                        field_name="CTYPE",
                                        field_value="getType( !CONTOUR! )",
                                        code_block="def getType(contour):\n\n   type = 2\n\n   if contour%10 == 0:\n\n      type = 10\n\n   if contour%20 == 0:\n\n      type = 20\n\n   if contour%50 == 0:\n      type = 50\n   if contour%100 == 0:\n      type = 100\n   if contour%500 == 0:\n      type = 500\n   if contour%1000 == 0:\n      type = 1000\n   if contour%5000 == 0:\n      type = 5000\n   return type",
                                        add_index=False)
            
            Utility.addAndCalcFieldLong(dataset_path=clip_contours,
                                        field_name="INDEX",
                                        field_value="getType( !CONTOUR! )",
                                        code_block="def getType(contour):\n\n   type = 0\n\n   if contour%" + str(int(cont_int * 5)) + " == 0:\n\n      type = 1\n   return type",
                                        add_index=False)
    #             Utility.addAndCalcFieldText(dataset_path=clip_contours, 
    #                                         field_name="LastMergedFC",
    #                                         field_length=100,
    #                                         field_value=name,
    #                                         add_index=False)
    #             Utility.addAndCalcFieldText(dataset_path=clip_contours, 
    #                                         field_name="ValidationCheck",
    #                                         field_length=100,
    #                                         field_value='"'+name+'"',
    #                                         add_index=False)
            Utility.addAndCalcFieldText(dataset_path=clip_contours,
                                        field_name="UNITS",
                                        field_length=20,
                                        field_value='"' + CONTOUR_UNIT + '"',
                                        add_index=False)
            Utility.addAndCalcFieldText(dataset_path=clip_contours,
                                        field_name="name",
                                        field_length=79,
                                        field_value='"' + name + '"',
                                        add_index=False)
            a = doTime(a, '\t' + name + ' ' + index + ': Added fields to ' + clip_contours)
                
            try:
                arcpy.DeleteField_management(in_table=clip_contours, drop_field="ID;InLine_FID;SimLnFlag;MaxSimpTol;MinSimpTol")
                a = doTime(a, '\t' + name + ' ' + index + ': Deleted fields from ' + clip_contours)
            except:
                pass
            
            doTime(aa, 'FINISHED ' + name + ' ' + index)
            created = True

        except Exception as e:
            arcpy.AddMessage('\t\tProcess Dropped: ' + name)
            arcpy.AddMessage('\t\tException: ' + str(e))
            if tries > TRIES_ALLOWED:
                arcpy.AddError('Too many tries, Dropped: {}'.format(name))
    try:
        arcpy.AddMessage("Checking in licenses")                        
        arcpy.CheckInExtension("3D")
        arcpy.CheckInExtension("Spatial")
    except:
        pass


def handle_results(scratch_dir, contour_dir):

    output_folders = os.listdir(scratch_dir)

    merge_list = []

    for folder in output_folders:
        cont = os.path.join(scratch_dir, 'O11_ClipCont_' + folder + '.shp')
        if arcpy.Exists(cont):
            merge_list.append(cont)

    a = datetime.now()
    merge_name = os.path.join(contour_dir, CONTOUR_NAME_OCS)
    project_name = os.path.join(contour_dir, CONTOUR_NAME_WM)
    if arcpy.Exists(merge_name):
        arcpy.AddMessage("Merged OCS Contours exist: " + merge_name)
    else:
        # Delete the projected since they might have changed with the merge
        deleteFileIfExists(project_name, True)
        arcpy.Merge_management(merge_list, merge_name)
        try:
            arcpy.DeleteField_management(in_table=merge_name, drop_field="ID;InLine_FID;SimLnFlag;MaxSimpTol;MinSimpTol")
        except:
            pass
        doTime(a, 'Merged ' + str(len(merge_list)) + ' Multiprocessing Results into ' + merge_name)
    
    
    if arcpy.Exists(project_name):
        arcpy.AddMessage("Projected Contours exist: " + project_name)
    else:
        arcpy.Project_management(
            merge_name,
            project_name,
            WEB_AUX_SPHERE
        )
        try:
            arcpy.DeleteField_management(in_table=merge_name, drop_field="ID;InLine_FID;SimLnFlag;MaxSimpTol;MinSimpTol")
        except:
            pass
        doTime(a, 'Projected Multiprocessing Results to ' + project_name)

def isProcessFile(f_name, scratch_dir):
    process_file = False
    if f_name is not None:
        cont = os.path.join(scratch_dir, 'O11_ClipCont_' + f_name + '.shp')
        if not os.path.exists(cont):
            arcpy.AddMessage("PROCESS (Missing): " + cont)
            process_file = True
        else:
            try:
                rows = [row for row in arcpy.da.SearchCursor(cont, "OID@")]  # @UndefinedVariable
                rows = len(rows)
                if rows <= 0:
                    arcpy.AddMessage("PROCESS (0 Rows): " + cont)
                    arcpy.Delete_management(cont)
                    process_file = True
            except:
                arcpy.AddMessage("\tFailed to isProcess contour file: " + cont)
                process_file = True

    return process_file



def createTiledContours(ref_md, cont_int, cont_unit, raster_vertical_unit, smooth_unit, scratch_path, run_dict, run_again=True):
    arcpy.AddMessage("---- Creating Contours on {} -----".format(len(run_dict.items())))
    # Map Generate Contour Function to Footprints
    pool = Pool(processes=cpu_count() - CPU_HANDICAP)
    pool.map(
        partial(
            generate_contour,
            ref_md,
            cont_int,
            cont_unit,
            raster_vertical_unit,
            smooth_unit,
            scratch_path
        ),
        run_dict.items()
    )
    pool.close()
    pool.join()

    if run_again:
        # run again to re-create missing tiles if one or more dropped
        # @TODO: Figure out why we have to do this!!
        createTiledContours(ref_md, cont_int, cont_unit, raster_vertical_unit, smooth_unit, scratch_path, run_dict, False)

def processJob(ProjectJob, project, ProjectUID):
    start = time.time()
    a = start
    # From ContourConfig
    cont_int = CONTOUR_INTERVAL
    cont_unit = CONTOUR_UNIT
    smooth_unit = CONTOUR_SMOOTH_UNIT
    distance_to_clip_md = DISTANCE_TO_CLIP_MOSAIC_DATASET
    distance_to_clip_contours = DISTANCE_TO_CLIP_CONTOURS
    
    ProjectFolder = ProjectFolders.getProjectFolderFromDBRow(ProjectJob, project)
    derived_folder = ProjectFolder.derived.path
    published_folder = ProjectFolder.published.path
#     project_id = ProjectJob.getProjectID(project)
    ProjectFolder = ProjectFolders.getProjectFolderFromDBRow(ProjectJob, project)
    contour_folder = ProjectFolder.derived.contour_path
#     raster_folder = ProjectFolder.published.demLastTiff_path
    
    
    filegdb_name, filegdb_ext = os.path.splitext(ProjectFolder.published.fgdb_name)  # @UnusedVariable    
    publish_filegdb_name = "{}_{}.gdb".format(filegdb_name, DTM)
    
#     published_path = os.path.join(published_folder, DTM) 
    published_filegdb_path = os.path.join(published_folder, publish_filegdb_name)
    md = os.path.join(published_filegdb_path, "{}{}".format(DTM, OCS))
    
    derived_filegdb_path = os.path.join(derived_folder, ProjectFolder.derived.fgdb_name)
    ref_md = os.path.join(derived_filegdb_path, "ContourPrep")
    ft_prints = A05_C_ConsolidateRasterInfo.getRasterFootprintPath(derived_filegdb_path, DTM)

    ###############################################################################
    # CMDR Class Variables & Inputs From Previous Jobs
    ###############################################################################
#     contour_folder    = r'C:\Users\jeff8977\Desktop\NGCE\OK_Sugar_Testing\DERIVED\CONTOURS'
#     published_folder  = r'C:\Users\jeff8977\Desktop\NGCE\OK_Sugar_Testing\PUBLISHED'
#     raster_folder     = r'C:\Users\jeff8977\Desktop\NGCE\OK_Sugar_Testing\PUBLISHED\DTM'
#     project_id = r'OK_SugarCreek_2008'

#     md = r'C:\Users\jeff8977\Desktop\NGCE\OK_Sugar\DERIVED\CONTOURS\Temp_MD_origCS.gdb\MD'
#     ft_prints = r'C:\Users\jeff8977\Desktop\NGCE\OK_Sugar\DERIVED\CONTOURS\Temp_MD_origCS.gdb\MD_Footprints'

    raster_vertical_unit = 'MT'
    foot_fields = [FIELD_INFO[V_UNIT][0]]
    for row in arcpy.da.SearchCursor(ft_prints, foot_fields):  # @UndefinedVariable
        raster_vertical_unit = row[0]
        break
    del row
    arcpy.AddMessage("Got input raster vertical unit: {}".format(raster_vertical_unit))
    
#     PYTHON_EXE = os.path.join(r'C:\Python27\ArcGISx6410.5', 'pythonw.exe')
# 
#     jobId = '1'
    ###############################################################################
    ###############################################################################

    try:
        a = datetime.now()
        # Generate Script Workspaces
        contour_gdb, scratch_path = generate_con_workspace(contour_folder)
        a = doTime(a, "Created Contour Workspace\n\t{}\n\t{}".format(contour_gdb, scratch_path))
        
        # Create referenced DTM mosaic with the pixel pre-setup for contour output
        createRefDTMMosaic(md, ref_md, raster_vertical_unit)
        
        # Collect Processing Extents
        run_dict = create_iterable(scratch_path, ft_prints, distance_to_clip_md, distance_to_clip_contours)
        
        
    except Exception as e:
        arcpy.AddWarning('Exception Raised During Script Initialization')
        arcpy.AddWarning('Exception: ' + str(e))

    
    try:
        createTiledContours(ref_md, cont_int, cont_unit, raster_vertical_unit, smooth_unit, scratch_path, run_dict)
 
        # Merge Contours
        handle_results(scratch_path, contour_gdb)

    except Exception as e:
        arcpy.AddMessage('Exception Raised During Multiprocessing')
        arcpy.AddError('Exception: ' + str(e))

    finally:
        run = time.time() - start
        arcpy.AddMessage('Script Ran: ' + str(run))

def CreateContoursFromMD(strJobId):
    Utility.printArguments(["WMXJobID"],
                           [strJobId], "C01 CreateContoursFromMD")
    aa = datetime.now()
    
    project_job, project, strUID = getProjectFromWMXJobID(strJobId)  # @UnusedVariable
    
    processJob(project_job, project, strUID)
    
    doTime(aa, "Operation Complete: C01 Create Contours From MD")

if __name__ == '__main__':
    arcpy.env.overwriteOutput = True
    
    arcpy.AddMessage("Checking out licenses")
    arcpy.CheckOutExtension("3D")
    arcpy.CheckOutExtension("Spatial")
        
    if len(sys.argv) > 1:
        projId = sys.argv[1]

        CreateContoursFromMD(projId)
    else:
        # DEBUG
        UID = None  # field_ProjectJob_UID
        wmx_job_id = 1
        project_Id = "OK_SugarCreek_2008"
        alias = "Sugar Creek"
        alias_clean = "SugarCreek"
        state = "OK"
        year = 2008
        parent_dir = r"E:\NGCE\RasterDatasets"
        archive_dir = r"E:\NGCE\RasterDatasets"
        project_dir = r"E:\NGCE\RasterDatasets\OK_SugarCreek_2008"
        project_AOI = None
        project_job = ProjectJob()
        project = [
                   UID,  # field_ProjectJob_UID
                   wmx_job_id,  # field_ProjectJob_WMXJobID,
                   project_Id,  # field_ProjectJob_ProjID,
                   alias,  # field_ProjectJob_Alias
                   alias_clean,  # field_ProjectJob_AliasClean
                   state ,  # field_ProjectJob_State
                   year ,  # field_ProjectJob_Year
                   parent_dir,  # field_ProjectJob_ParentDir
                   archive_dir,  # field_ProjectJob_ArchDir
                   project_dir,  # field_ProjectJob_ProjDir
                   project_AOI  # field_ProjectJob_SHAPE
                   ]
        
        processJob(project_job, project, UID)

    
    try:
        arcpy.AddMessage("Checking in licenses")                        
        arcpy.CheckInExtension("3D")
        arcpy.CheckInExtension("Spatial")
    except:
        pass


    
    
