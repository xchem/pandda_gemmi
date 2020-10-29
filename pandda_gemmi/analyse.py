import os
from pandda_gemmi.constants import PANDDA_LOG_FILE
from typing import Dict
import time
import psutil
import pickle
from shlex import split
from pprint import PrettyPrinter
from pathlib import Path

import numpy as np

import joblib

from pandda_gemmi.config import Config
from pandda_gemmi import logs
from pandda_gemmi.pandda_types import (PanDDAFSModel, Datasets, Reference, 
                                    Grid, Alignments, Shells, Xmaps, 
                                    XmapArray, Model, Dtag, Zmaps, Clusterings,
                                    Events, SiteTable, EventTable
                                    )


def main():
    ###################################################################
    # # Configuration
    ###################################################################
    print("Getting config")
    config: Config = Config.from_args()
    
    print("Initialising log...")
    pandda_log: logs.LogData = logs.LogData.initialise()
    pandda_log.config = config
    
    try:

        print("FSmodel building")
        pandda_fs_model: PanDDAFSModel = PanDDAFSModel.from_dir(config.input.data_dirs,
                                                                config.output.out_dir,
                                                                config.input.pdb_regex,
                                                                config.input.mtz_regex,
                                                                )
        pandda_fs_model.build()
        pandda_log.fs_log = logs.FSLog.from_pandda_fs_model(pandda_fs_model)
        
        print("Getting multiprocessor")
        mapper = joblib.Parallel(n_jobs=-2, 
                                      verbose=15,
                                      backend="multiprocessing",
                                       max_nbytes=None)
        print(mapper)
                
        ###################################################################
        # # Pre-pandda
        ###################################################################
        
        # Get datasets
        print("Loading datasets")
        datasets_initial: Datasets = Datasets.from_dir(pandda_fs_model)
        pandda_log.preprocessing_log.initial_datasets_log = logs.InitialDatasetLog.from_initial_datasets(datasets_initial)

        # Initial filters
        print("Filtering invalid datasaets")
        datasets_invalid: Datasets = datasets_initial.remove_invalid_structure_factor_datasets(
        config.params.diffraction_data.structure_factors)
        pandda_log.preprocessing_log.invalid_datasets_log = logs.InvalidDatasetLog.from_datasets(datasets_initial, datasets_invalid)

        datasets_low_res: Datasets = datasets_invalid.remove_low_resolution_datasets(
            config.params.diffraction_data.low_resolution_completeness)
        pandda_log.preprocessing_log.low_res_datasets_log = logs.InvalidDatasetLog.from_datasets(datasets_invalid, datasets_low_res)

        datasets_rfree: Datasets = datasets_low_res.remove_bad_rfree(config.params.filtering.max_rfree)
        pandda_log.preprocessing_log.rfree_datasets_log = logs.RFreeDatasetLog.from_datasets(datasets_low_res, datasets_rfree)

        datasets_wilson: Datasets = datasets_rfree.remove_bad_wilson(config.params.filtering.max_wilson_plot_z_score)  # TODO
        pandda_log.preprocessing_log.wilson_datasets_log = logs.WilsonDatasetLog.from_datasets(datasets_rfree, datasets_wilson)

        # Select refernce
        print("Getting reference")
        reference: Reference = Reference.from_datasets(datasets_wilson)
        pandda_log.reference_log = logs.ReferenceLog.from_reference(reference)


        # Post-reference filters
        print("smoothing")
        datasets_smoother: Datasets = datasets_wilson.smooth_datasets(reference, 
                                                    structure_factors=config.params.diffraction_data.structure_factors,
                                                    mapper=mapper,
                                                    )  
        pandda_log.preprocessing_log.smoothing_datasets_log = logs.SmoothingDatasetLog.from_datasets(datasets_smoother)

        print("Removing dissimilar models")
        datasets_diss_struc: Datasets = datasets_smoother.remove_dissimilar_models(reference,
                                                            config.params.filtering.max_rmsd_to_reference,
                                                            )
        pandda_log.preprocessing_log.struc_datasets_log = logs.StrucDatasetLog.from_datasets(datasets_smoother, datasets_diss_struc)

        datasets_diss_space: Datasets = datasets_diss_struc.remove_dissimilar_space_groups(reference)
        pandda_log.preprocessing_log.space_datasets_log = logs.SpaceDatasetLog.from_datasets(datasets_diss_struc, datasets_diss_space)

        datasets = datasets_diss_space

        # Grid
        print("Getting grid")
        grid: Grid = Grid.from_reference(reference,
                                config.params.masks.outer_mask,
                                    config.params.masks.inner_mask_symmetry,
                                        sample_rate=3.0,
                                    )
        pandda_log.grid_log = logs.GridLog.from_grid(grid)

        print("Getting alignments")
        alignments: Alignments = Alignments.from_datasets(reference,
                                                        datasets,
                                                        )
        pandda_log.alignments_log = logs.AlignmentsLog.from_alignments(alignments)

                
        ###################################################################
        # # Process shells
        ###################################################################
        shells = Shells.from_datasets(
            datasets, 
            config.params.resolution_binning.min_characterisation_datasets,
            config.params.resolution_binning.max_shell_datasets,
            config.params.resolution_binning.high_res_increment)

        all_events = {}
        for shell in shells:
            print(f"Working on shell: {shell}")
            pandda_log.shells_log[shell.number] = logs.ShellLog.from_shell(shell)


            # Seperate out test and train datasets
            shell_datasets: Datasets = datasets.from_dtags(shell.all_dtags)

            print("Truncating datasets")
            shell_truncated_datasets: Datasets = shell_datasets.truncate(resolution=shell.res_min,
                                                                        structure_factors=config.params.diffraction_data.structure_factors,
                                                                        )

            # Assign datasets
            shell_train_datasets: Datasets = shell_truncated_datasets.from_dtags(shell.train_dtags)
            shell_test_datasets: Datasets = shell_truncated_datasets.from_dtags(shell.test_dtags)

            # Generate aligned xmaps
            print("Loading xmaps")
            xmaps = Xmaps.from_aligned_datasets_c(
                shell_truncated_datasets, 
                alignments, 
                grid,
                config.params.diffraction_data.structure_factors, 
                sample_rate=config.params.diffraction_data.sample_rate,
                mapper=False,
                )

            # Seperate out test and train maps
            shell_train_xmaps: Xmaps = xmaps.from_dtags(shell.train_dtags)
            shell_test_xmaps: Xmaps = xmaps.from_dtags(shell.test_dtags)

            # Get arrays for model
            print("Getting xmap arrays...")        
            masked_xmap_array: XmapArray = XmapArray.from_xmaps(xmaps,
                                        grid,
                                        )
            masked_train_xmap_array: XmapArray = masked_xmap_array.from_dtags(shell.train_dtags)
            masked_test_xmap_array: XmapArray = masked_xmap_array.from_dtags(shell.test_dtags)

            # Determine the parameters of the model to find outlying electron density
            print("Fitting model")
            mean_array: np.ndarray = Model.mean_from_xmap_array(masked_train_xmap_array,
                                            )

            print("fitting sigma i")
            sigma_is: Dict[Dtag, float] = Model.sigma_is_from_xmap_array(masked_xmap_array,
                                                        mean_array,
                                                    1.5,
                                                    )
            pandda_log.shells_log[shell.number].sigma_is = {dtag.dtag: sigma_i 
                                                            for dtag, sigma_i 
                                                            in sigma_is.items()}

            print("fitting sigma s m")
            sigma_s_m: np.ndarray = Model.sigma_sms_from_xmaps(masked_train_xmap_array,
                                                        mean_array,
                                                        sigma_is,
                                                        )

            model: Model = Model.from_mean_is_sms(mean_array,
                                sigma_is,
                                sigma_s_m,
                                grid,
                                )

            # Calculate z maps
            print("Getting zmaps")
            zmaps: Zmaps = Zmaps.from_xmaps(model=model,
                                        xmaps=shell_test_xmaps,
                                        )

            # Get the clustered electron desnity outliers
            print("clusting")
            clusterings: Clusterings = Clusterings.from_Zmaps(
                zmaps, 
                reference,
                grid,
                config.params.masks.contour_level,
                cluster_cutoff_distance_multiplier=config.params.blob_finding.cluster_cutoff_distance_multiplier,
                mapper=mapper,
                )
            pandda_log.shells_log[shell.number].initial_clusters = logs.ClusteringsLog.from_clusters(
                clusterings, grid)

            # Filter out small clusters
            clusterings_large: Clusterings = clusterings.filter_size(grid,
                                                                    config.params.blob_finding.min_blob_volume,
                                                                    )
            pandda_log.shells_log[shell.number].large_clusters = logs.ClusteringsLog.from_clusters(
                clusterings_large, grid)

            # Filter out weak clusters (low peak z score)
            clusterings_peaked: Clusterings = clusterings_large.filter_peak(grid,
                                                config.params.blob_finding.min_blob_z_peak)
            pandda_log.shells_log[shell.number].peaked_clusters = logs.ClusteringsLog.from_clusters(
                clusterings_peaked, grid)
            
            clusterings_merged = clusterings_peaked.merge_clusters()
            pandda_log.shells_log[shell.number].clusterings_merged = logs.ClusteringsLog.from_clusters(
                clusterings_merged, grid)

            # Calculate the shell events
            print("getting events")
            events: Events = Events.from_clusters(clusterings_merged, model, xmaps, grid, 1.732)
            pandda_log.shells_log[shell.number].events = logs.EventsLog.from_events(events, grid)
            print(pandda_log.shells_log[shell.number].events)

            # Save the z maps
            print("saving zmaps")
            for dtag in zmaps:
                zmap = zmaps[dtag]
                pandda_fs_model.processed_datasets.processed_datasets[dtag].z_map_file.save(zmap)
            # Save the x maps
            print("Saving xmaps")
            for dtag in xmaps:
                xmap = xmaps[dtag]
                path = pandda_fs_model.processed_datasets.processed_datasets[dtag].path / "xmap.ccp4"
                xmap.save(path)
                
            # Save the event maps!
            print("print events")
            events.save_event_maps(shell_truncated_datasets,
                                alignments,
                                xmaps,
                                model,
                                pandda_fs_model,
                                grid,
                                config.params.diffraction_data.structure_factors,
                                config.params.masks.outer_mask,
                                config.params.masks.inner_mask_symmetry,
                                multiprocess=True,
                                )
            
            for event_id in events:
                all_events[event_id] = events[event_id]
                

        all_events_events = Events.from_all_events(all_events, grid, 1.7)
        # Get the sites and output a csv of them
        site_table: SiteTable = SiteTable.from_events(all_events_events, 1.7)
        site_table.save(pandda_fs_model.analyses.pandda_analyse_sites_file)
        pandda_log.sites_log = logs.SitesLog.from_sites(site_table)

        # Output a csv of the events
        event_table: EventTable = EventTable.from_events(all_events_events)
        event_table.save(pandda_fs_model.analyses.pandda_analyse_events_file)
        pandda_log.events_log = logs.EventsLog.from_events(all_events_events,
                                                        grid,
                                                        )

    except Exception as e:
        pandda_log.exception = str(e)
        pandda_log.print()
        pandda_log.save_json(config.output.out_dir / PANDDA_LOG_FILE)
        




if __name__ == '__main__':

    main()


