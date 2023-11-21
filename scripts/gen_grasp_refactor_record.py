import time
import numpy as np
import trimesh as tr
import tyro

import os
import os.path as osp
from pathlib import Path
import tqdm
import open3d as o3d
import matplotlib
from typing import List, Dict, Tuple

import torch
device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

import viser
import viser.transforms as tf
from autolab_core import RigidTransform

from graspnetAPI import GraspGroup, Grasp
from nerfstudio.pipelines.base_pipeline import Pipeline

from robot_lerf.graspnet_baseline.load_ns_model import NerfstudioWrapper, RealsenseCamera
from robot_lerf.graspnet_baseline.graspnet_module import GraspNetModule
from robot_lerf.capture_utils import _generate_hemi
from robot_lerf.grasp_planner_cmk import UR5GraspPlanner # , UR5_HOME_JOINT, ARM_JOINT_NAMES

import capture as lerf_capture


def get_relevancy_pointcloud(ns_wrapper: NerfstudioWrapper, **kwargs):
    """Get relevancy pointcloud, used to get semantic score

    Args:
        ns_wrapper (NerfstudioWrapper): nerf scene

    Returns:
        o3d.utility.Vector3DVector: points in pointcloud (xyz)
        np.ndarray: relevancy score
    """
    lerf_xyz, lerf_relevancy = [], []
    center_pos_matrix = np.array([[ 1., 0., 0., 0.45], [0., -0.70710678,  0.70710678, -0.28284271],[ 0, -0.70710678, -0.70710678,  0.10284271]])
    c2w = ns_wrapper.visercam_to_ns(center_pos_matrix)
    rscam = RealsenseCamera.get_camera(c2w, downscale=1/4)
    lerf_pcd, lerf_relevancy, dino_pcd, pick_object_pt, place_pt = ns_wrapper.get_lerf_pointcloud(rscam)
    pick_object_pt = tr.transformations.transform_points(pick_object_pt.unsqueeze(0).cpu().numpy(), ns_wrapper.applied_transform).squeeze()
    if place_pt is not None:
        place_pt = tr.transformations.transform_points(place_pt.unsqueeze(0).cpu().numpy(), ns_wrapper.applied_transform).squeeze()
    lerf_pcd.points = o3d.utility.Vector3dVector(tr.transformations.transform_points(np.asarray(lerf_pcd.points), ns_wrapper.applied_transform)) #nerfstudio pc to world/viser pc
    lerf_xyz = np.asarray(lerf_pcd.points)
    dino_pcd.points = o3d.utility.Vector3dVector(tr.transformations.transform_points(np.asarray(dino_pcd.points), ns_wrapper.applied_transform))
    lerf_points_o3d = lerf_pcd.points
    dino_points_o3d = dino_pcd.points

    return lerf_points_o3d, lerf_relevancy, dino_points_o3d, pick_object_pt, place_pt

def get_grasps(
    graspnet: GraspNetModule,
    world_pointcloud: tr.PointCloud,
    hemisphere: List[RigidTransform],
    graspnet_batch_size: int = 15,
    ) -> GraspGroup:
    """Get grasps from graspnet, as images taken from the hemisphere
    
    Args: 
        graspnet (GraspNetModule): graspnet module
        world_pointcloud (tr.PointCloud): world pointcloud
        hemisphere (List[RigidTransform]): list of camera poses
    
    Returns:
        GraspGroup: grasps
    """
    torch.cuda.empty_cache()
    gg_all = None
    for i in range(0, len(hemisphere), graspnet_batch_size):
        start = time.time()
        ind_range = range(i, min(i+graspnet_batch_size, len(hemisphere)))
        rgbd_cropped_list = []
        for j in ind_range:
            c2w = hemisphere[j].matrix[:3,:]
            rgbd_cropped = world_pointcloud.copy()
            rgbd_cropped.vertices = tr.transformations.transform_points(
                rgbd_cropped.vertices,
                np.linalg.inv(np.concatenate([c2w, [[0, 0, 0, 1]]], axis=0))
            )
            rgbd_cropped_list.append(rgbd_cropped)
        print("Transform time: ", time.time() - start)

        gg_list = graspnet(rgbd_cropped_list)
        for g_ind, gg in enumerate(gg_list):
            c2w = hemisphere[i + g_ind].matrix[:3,:]
            gg.transform(np.concatenate([c2w, [[0, 0, 0, 1]]], axis=0))
        print(f"Grasp pred time: {time.time() - start:.2f}s")
        start = time.time()

        gg_all_curr = gg_list[0]
        for gg in gg_list[1:]:
            gg_all_curr.add(gg)
        gg = gg_all_curr

        # If the grasps are too close to the ground, then lift them a bit.
        # This is hardcoded though, so it might not work for all scenes
        gg_translations = gg.translations
        gg_translations[gg_translations[:, 2] < -0.16] += np.tile(np.array([0, 0, 0.01]), ((gg_translations[:, 2] < -0.16).sum(), 1))
        gg.translations = gg_translations
        # gg[gg.translations[:, 2] < -0.16].translations += np.tile(np.array([0, 0, 0.04]), ((gg.translations[:, 2] < -0.16).sum(), 1))
        gg = gg[(gg.translations[:, 0] > 0.22)] #& (gg.translations[:, 2] < 0.05)]

        gg = gg[np.abs(gg.rotation_matrices[:, :, 1][:, 2]) < 0.5]

        # gg = gg[gg.scores > 0.6]
        if len(gg) == 0:
            continue

        gg = gg.nms(translation_thresh=0.01, rotation_thresh=30.0/180.0*np.pi)

        # select grasps that are not too close to the table
        # Currently, this function does general grasp filtering (using collision detection, grasp includes non-table components, ...)
        gg = graspnet.local_collision_detection(gg)

        print(f"Collision detection time: {time.time() - start:.2f}s")
        print(f"Post proc time: {time.time() - start:.2f}s")
        if gg_all is None:
            gg_all = gg
        else:
            gg_all.add(gg)

    if gg_all is None:
        return GraspGroup()
    
    gg_all = gg_all.nms(translation_thresh=0.01, rotation_thresh=30.0/180.0*np.pi)
    gg_all.sort_by_score()
    torch.cuda.empty_cache()

    return gg_all

def instantiate_scene_from_model(
    server: viser.ViserServer,
    graspnet_ckpt: str,
    floor_height: float,
    scene_name: str,
    config_path: str = None,
    pipeline: Pipeline = None,
) -> Tuple[NerfstudioWrapper, tr.PointCloud, tr.PointCloud, np.ndarray, GraspNetModule, GraspGroup, np.ndarray]:
    """Instantiate a scene from a NerfStudio model, as well as the associated GraspNetModule.

    Args:
        server (viser.ViserServer): viser server, used for visualization
        config_path (str): Nerfstudio model config path
        graspnet_ckpt (str): GraspNet checkpoint path
        floor_height (float): floor height

    Returns:
        Tuple[NerfstudioWrapper, tr.PointCloud, tr.PointCloud, np.ndarray, GraspNetModule]:
         - world_pointcloud: world pointcloud
         - global_pointcloud: nerf pointcloud (not cropped)
         - table_center: table center
         - graspnet: graspnet module
         - grasps: GraspGroup
         - overall_scores: overall scores

    """
    if config_path is not None:
        ns_wrapper = NerfstudioWrapper(scene_path=config_path)
    elif pipeline is not None:
        ns_wrapper = NerfstudioWrapper(pipeline=pipeline)
    else:
        raise ValueError("Must provide either scene_path or pipeline")
    
    world_pointcloud, global_pointcloud, table_center = ns_wrapper.create_pointcloud()

    graspnet = GraspNetModule()
    graspnet.init_net(graspnet_ckpt, global_pointcloud, cylinder_radius=0.04, floor_height=floor_height)

    server.add_point_cloud(
        name=f"/world_pointcloud",
        points=np.asarray(world_pointcloud.vertices),
        colors=np.asarray(world_pointcloud.visual.vertex_colors[:, :3]),
        point_size=0.002,
    )

    # server.add_point_cloud(
    #     name=f"/coll_pointcloud",
    #     points=graspnet.pointcloud_vertices,
    #     colors=np.repeat(np.array([[0, 1, 0]]), len(graspnet.pointcloud_vertices), axis=0),
    #     point_size=0.002,
    #     visible=False
    # )
    # world_pointcloud.export("world_pointcloud.ply")

    hemi_radius = 2
    hemi_theta_N = 15
    hemi_phi_N = 15
    hemi_th_range = 90
    hemi_phi_down = 0
    hemi_phi_up = 70

    if osp.exists(f"outputs/{scene_name}/grasps.npy"):
        grasps = GraspGroup(np.load(f"outputs/{scene_name}/grasps.npy"))
    else:
        grasp_hemisphere = _generate_hemi(
            hemi_radius,hemi_theta_N,hemi_phi_N,
            (np.deg2rad(-hemi_th_range),np.deg2rad(hemi_th_range)),
            (np.deg2rad(hemi_phi_down),np.deg2rad(hemi_phi_up)),
            center_pos=table_center,look_pos=table_center
            )
        grasps = get_grasps(graspnet, world_pointcloud, grasp_hemisphere)
        grasps.save_npy(f"outputs/{scene_name}/grasps.npy")

    # # just add all the grasps lol
    # for i, grasp in enumerate(grasps):
    #     add_grasps(server, grasp, i, 0.5)

    return (
        ns_wrapper,
        world_pointcloud,
        global_pointcloud,
        table_center,
        grasps,
        np.array(grasps.scores)
    )

def add_grasps(
    server: viser.ViserServer,
    grasp: Grasp,
    ind: int,
    score: float,
) -> Tuple[viser.SceneNodeHandle, viser.SceneNodeHandle, viser.SceneNodeHandle, viser.SceneNodeHandle]:
    """Curry function for adding grasps to the scene.

    Args:
        server (viser.ViserServer): _description_
        grasp (Grasp): _description_
        ind (int): _description_
        score (float): The color is based on the score -- put it from 0 to 1

    Returns:
        Tuple[viser.SceneNodeHandle, viser.SceneNodeHandle, viser.SceneNodeHandle]:
         - frame_handle: [Grasp frame (graspnetAPI)] to [world]
         - grasp_handle: mesh
         - ur5_handle: [UR5 frame (EE)] to [Grasp frame (graspnetAPI)]
    """
    colormap = matplotlib.colormaps['RdYlGn']
    robot_frame_R = RigidTransform(
        rotation=RigidTransform.y_axis_rotation(np.pi/2) @ RigidTransform.z_axis_rotation(np.pi/2)
    )

    default_grasp = Grasp()
    default_grasp.depth = grasp.depth
    default_grasp.width = grasp.width
    default_grasp.height = grasp.height
    default_grasp = default_grasp.to_open3d_geometry()

    frame_handle = server.add_frame(
        name=f'/lerf/grasps_{ind}',
        wxyz=tf.SO3.from_matrix(grasp.rotation_matrix).wxyz,
        position=grasp.translation,
        show_axes=False
    )
    grasp_handle = server.add_mesh(
        name=f'/lerf/grasps_{ind}/mesh',
        vertices=np.asarray(default_grasp.vertices),
        faces=np.asarray(default_grasp.triangles),
        color=colormap(score)[:3],
    )
    ur5_handle = server.add_frame(
        name=f'/lerf/grasps_{ind}/ur5',
        wxyz=robot_frame_R.quaternion,
        # position=np.array([0.03, 0, 0]),
        position=np.array([grasp.depth-0.015, 0, 0]),
        axes_length=0.05,
        axes_radius=0.002,
        show_axes=True,
        visible=False
    )
    return frame_handle, grasp_handle, ur5_handle


def get_bbox_from_grasp(gg: Grasp) -> o3d.geometry.OrientedBoundingBox:
    center = gg.translation
    R = gg.rotation_matrix
    H= np.eye(4)
    H[:3,:3] = R
    H[:3,3] = center
    extent=np.array((gg.depth,gg.width,gg.height))
    box = o3d.geometry.OrientedBoundingBox(center,H[:3,:3],extent)
    return box


def main(
    config_path: str = None,  # Nerfstudio model config path, of format outputs/.../config.yml; if None, make sure you capture!
    graspnet_ckpt: str = 'robot_lerf/graspnet_baseline/logs/log_kn/checkpoint.tar',  # GraspNet checkpoint path
    urdf_path: str = "pybullet_ur5_robotiq/urdf/ur5_robotiq_85.urdf"
    ):
    path = "outputs/fragile_take2/lerf-lite/one_model/config.yml"
    query = "camera; camera strap"
    server = viser.ViserServer()
    # server.add_frame(
    #     name="/world",
    #     axes_length=0.05,
    #     axes_radius=0.01,
    #     show_axes=True
    # )

    # # Create UR5 robot
    grasp_planner = UR5GraspPlanner(Path(urdf_path))
    # ur5_frame = server.add_frame(
    #     name=f"/ur5",
    #     wxyz=tf.SO3.from_z_radians(np.pi).wxyz,
    #     position=np.array([0, 0, 0]),
    #     show_axes=False
    # )
    # grasp_planner.create_robot(server, root_transform=ur5_frame, use_visual=True)
    # grasp_planner.goto_joints(grasp_planner.UR5_HOME_JOINT) # , show_collbodies=True)

    # Create all necessary global variables
    grasps, grasps_dict, lerf_scores, geom_scores, overall_scores, pick_object_pt, place_point, grasp_point, fin_grasp = None, {}, [], [], [], None, None, None, None
    lerf_points_o3d, lerf_relevancy = None, None
    traj = None


    
    if config_path is not None:
        ns_wrapper, world_pointcloud, global_pointcloud, table_center, grasps, overall_scores = instantiate_scene_from_model(
            server,
            graspnet_ckpt,
            config_path=config_path,
            scene_name=config_path.split('/')[-4],
            floor_height=UR5GraspPlanner.FLOOR_HEIGHT
        )
    else:
        ns_wrapper, world_pointcloud, global_pointcloud, table_center, grasps, overall_scores = None, None, None, None, None, None


    with server.record(Path("viser_assets/" + "test" + ".viser")):
        ns_wrapper, world_pointcloud, global_pointcloud, table_center, grasps, overall_scores = instantiate_scene_from_model(
                server,
                graspnet_ckpt,
                floor_height=UR5GraspPlanner.FLOOR_HEIGHT,
                config_path=path,
                scene_name=path.split('/')[-4],
            )
        
        lerf_word = query.split(";")
        print("Calculating LERF query for: ", lerf_word)
        # Get the LERF activation poisntcloud for the given query
        ns_wrapper.pipeline.model.clip_scales.value = False
        ns_wrapper.pipeline.image_encoder.set_positives(lerf_word)
        lerf_points_o3d, lerf_relevancy, dino_pcd, pick_object_pt, place_point = get_relevancy_pointcloud(ns_wrapper, table_center=table_center)
        # Visualize the relevancy pointcloud 

        lerf_scores = []
        geom_scores = []
        for i, grasp in enumerate(grasps):
            box = get_bbox_from_grasp(grasp)
            # get the indices of the lerf_xyz pointcloud which lie inside the box and avg
            pts = box.get_point_indices_within_bounding_box(lerf_points_o3d)
            pts1 = box.get_point_indices_within_bounding_box(dino_pcd)
            if len(pts) == 0:
                score = 0.0
            else:
                # score = lerf_relevancy[pts].mean()
                score = np.median(lerf_relevancy[pts].squeeze()).item()
            score1 = grasp.score
            if len(pts1) == 0:
                score1 = 0.0
            
            lerf_scores.append(score)
            geom_scores.append(score1)

        #All visualization stuff
        lerf_scores = np.array(lerf_scores)
        lerf_scores /= lerf_relevancy.max()

        lerf_weight = 0.95
        geom_weight = 1.0 - lerf_weight
        # Update the scores...
        if lerf_scores is None or len(lerf_scores) == 0:
            scores = geom_scores
        elif lerf_weight == 1.0:
            scores = lerf_scores
        else:
            scores = (lerf_weight)*np.array(lerf_scores) + (geom_weight)*np.array(geom_scores)
        if len(scores) == 0:
            print("Need to run LERF query first!")
            return

        scores_threshold = np.quantile(scores, 0.95)
        # import pdb; pdb.set_trace()
        grasps_selected = [grasp for (ind, grasp) in enumerate(grasps) if scores[ind] > scores_threshold]
        inds_selected = [ind for (ind, grasp) in enumerate(grasps) if scores[ind] > scores_threshold]

        grasps_selected = GraspGroup(np.stack([grasp.grasp_array for grasp in grasps_selected]))
        # grasps_selected = grasps_selected.nms(translation_thresh=0.02, rotation_thresh=30.0/180.0*np.pi)
        grasps_selected = grasps_selected.sort_by_score()

        for ind, grasp_list in grasps_dict.items():
            for grasp in grasp_list:
                grasp.remove()
        grasps_dict = {}

        # min_score, max_score = scores[inds_selected].min(), scores[inds_selected].max()
        # print(scores[inds_selected].min(), scores[inds_selected].max())
        # scores -= min_score
        # scores /= max_score
        # print(scores[inds_selected].min(), scores[inds_selected].max())
        scores -= scores[inds_selected].min()
        scores /= scores[inds_selected].max()

        for ind, grasp in zip(inds_selected, grasps_selected):
            grasps_dict[ind] = add_grasps(server, grasp, ind, scores[ind])

        overall_scores = scores



    """
    All the functions that require the LERF load/train/save -- includes:
     - Load/save directory, assumes
        - output/NAME for LERF capture dataset
        - outputs/NAME for LERF training
        - the latest written config.yml for LERF loading
    
    Note that `lerf_dataset_path` affects the LERF capture capability.
    """
    with server.add_gui_folder("LERF load/train"):
        lerf_dataset_path = server.add_gui_text(
            label="Dataset path",
            initial_value="",
        )
        lerf_train_button = server.add_gui_button(
            label="Train/Load LERF",
        )
        lerf_reset_button = server.add_gui_button(
            label="Reset LERF",
        )
        if config_path is not None:
            lerf_dataset_path.disabled = True
            lerf_train_button.disabled = True

        @lerf_reset_button.on_click
        def _(_):
            nonlocal ns_wrapper, world_pointcloud, global_pointcloud, table_center, grasps, overall_scores
            if ns_wrapper is None:
                return
            # this doesn't actually seem to work, need to figure out why...
            del ns_wrapper.pipeline
            world_pointcloud, global_pointcloud, table_center, grasps, overall_scores = None, None, None, None, None
            torch.cuda.empty_cache()


        @lerf_train_button.on_click
        def _(_):
            nonlocal ns_wrapper, world_pointcloud, global_pointcloud, table_center, grasps, overall_scores
            data = Path(f'output/{lerf_dataset_path.value}/')
            if not osp.exists(data):
                print("data file doesn't exist; can't load/train; return")
                return
            
            lerf_train_button.disabled = True
            import nerfstudio.configs.method_configs as method_configs

            num_steps = 3200
            # num_steps = 10000
            config = method_configs.all_methods['lerf-lite']
            config.pipeline.datamanager.data = data
            config.max_num_iterations = num_steps+1
            config.steps_per_save = num_steps
            config.timestamp = "one_model"
            config.viewer.quit_on_train_completion = True

            if (
                osp.exists(config.get_base_dir()) and
                osp.exists(config.get_base_dir() / "nerfstudio_models")
            ):
                # config.load_dir = config.get_base_dir() / "nerfstudio_models"
                # config.load_step = num_steps
                # config.max_num_iterations = 0
                print("we are going to load a model")
                config_path = config.get_base_dir() / "config.yml"
                pipeline = None

            else:
                print("we are going to train a model")
                os.makedirs(config.get_base_dir(), exist_ok=True)
                config.save_config()

                trainer = config.setup(local_rank=0, world_size=1)
                trainer.setup()

                start = time.time()
                trainer.train()
                print(f"Training took {time.time() - start} seconds")
                pipeline = trainer.pipeline
                config_path = None
                pipeline.datamanager.config.patch_size = 1
                pipeline.datamanager.train_pixel_sampler = pipeline.datamanager._get_pixel_sampler(
                    pipeline.datamanager.train_dataset, 
                    pipeline.datamanager.config.train_num_rays_per_batch
                    )

            # config_path = sorted(Path("outputs").glob(f"{lerf_dataset_path.value}/*/config.yml"))[-1]
            ns_wrapper, world_pointcloud, global_pointcloud, table_center, grasps, overall_scores = instantiate_scene_from_model(
                server,
                graspnet_ckpt,
                floor_height=UR5GraspPlanner.FLOOR_HEIGHT,
                config_path=config_path,
                pipeline=pipeline,
                scene_name=lerf_dataset_path.value
            )
            lerf_train_button.disabled = False

    """
    All the functions that require the physical robot -- includes:
     - LERF capture
     - Grasp execution
    
    Be careful with this one, it will actually move the robot!
    This will only be executed if `use_robot` is checked in the GUI.

    Requires `lerf_dataset_path` to be set for the LERF capture.
    For the trajectory capture, `traj` is required to be set. (# of waypoints, 6)
    """
    with server.add_gui_folder("Robot"):
        robot_checkbox = server.add_gui_checkbox(
            label="Use Robot",
            initial_value=False
        )
        init_robot_button = server.add_gui_button(
            label="Initialize robot",
            disabled=True
        )
        lerf_capture_button = server.add_gui_button(
            label=f"Capture LERF",
            disabled=True
        )
        robot_button = server.add_gui_button(
            label="Move along trajectory",
            disabled=True
        )
        robot_joints_button = server.add_gui_button(
            label="move URDF to joints"
        )
        @robot_joints_button.on_click
        def _(_):
            nonlocal robot
            if robot is None:
                return
            joints = robot.get_joints()
            grasp_planner.goto_joints(joints[:6]) # , show_collbodies=True)
            grasp_planner.kinematics.set_config(joints)
            print("Computed pose from joints",grasp_planner.kinematics.ee_frame)
            print("Pose from robot",robot.get_pose())
            print()

            #IK testing
            target_pose = robot.get_pose()
            grasp_planner.kinematics.ikmod(target_pose.matrix)
            joints = grasp_planner.kinematics.config
            print("Computed joints from IK",joints)
            print("Joints from robot",robot.get_joints())

        
        robot = None

        @robot_checkbox.on_update
        def _(_):
            if robot_checkbox.value:
                init_robot_button.disabled = False
            else:
                init_robot_button.disabled = True

        @init_robot_button.on_click
        def _(_):
            nonlocal robot
            if not robot_checkbox.value:
                robot_button.disabled = True
                lerf_capture_button.disabled = True
                return
            if robot is not None:
                del robot
            robot_checkbox.disabled = True
            from ur5py.ur5 import UR5Robot
            robot = UR5Robot(gripper=True)
            # import pdb; pdb.set_trace()
            robot.set_tcp(RigidTransform(rotation=np.eye(3),translation=np.array([0,0,0.16])))
            time.sleep(0.5)
            robot.move_joint(grasp_planner.UR5_HOME_JOINT, vel=0.5, asyn=False)
            time.sleep(1)
            robot_button.disabled = False
            lerf_capture_button.disabled = False
            robot_checkbox.disabled = False

        @lerf_capture_button.on_click
        def _(_):
            nonlocal robot
            if (robot is None) or (not robot_checkbox.value):
                return
            if lerf_dataset_path.value == "":
                print("Please enter a valid path")
                return
            if osp.exists(lerf_dataset_path.value):
                print("The dataset already exists, choose a new name")
                return
            lerf_capture_button.disabled = True
            # this internally updates the TCP pose, so we need to reset it when we're done.
            lerf_capture.main("output/" + lerf_dataset_path.value, rob=robot)
            time.sleep(0.5)
            robot.set_tcp(RigidTransform(rotation=np.eye(3),translation=np.array([0,0,0.16])))
            time.sleep(0.5)
            lerf_capture_button.disabled = False

        @robot_button.on_click
        def _(_):
            if traj is None:
                return
            if (robot is None) or (not robot_checkbox.value):
                return
            if 'n' not in input("Go to grasp pose?"):
                robot.move_joint(grasp_planner.UR5_HOME_JOINT, vel=0.5, asyn=False)
                time.sleep(0.5)
                robot.gripper.open()
                # import pdb; pdb.set_trace()
                traj_goto, traj_lift = traj[:61,:], traj[61:,:]
                traj_goto = traj_goto[[0, 21, 41, -1], :]
                traj_lift = traj_lift[[0, len(traj_lift)//20, (len(traj_lift)//20) * 2, (len(traj_lift)//20) * 3, (len(traj_lift)//20) * 4, (len(traj_lift)//20) * 5, (len(traj_lift)//20) * 6, (len(traj_lift)//20) * 7, 
                                        len(traj_lift)//20* 8, (len(traj_lift)//20) * 9, (len(traj_lift)//20) * 10, (len(traj_lift)//20) * 11, (len(traj_lift)//20) * 12, (len(traj_lift)//20) * 13, (len(traj_lift)//20) * 14, 
                                        len(traj_lift)//20 * 15, (len(traj_lift)//20) * 16, (len(traj_lift)//20) * 17, (len(traj_lift)//20) * 18, (len(traj_lift)//20) * 19, -1], :]
                robot.move_joint_path(
                    traj_goto,
                    vels=[.2]*traj_goto.shape[0],
                    accs=[1.0]*traj_goto.shape[0],
                    blends = [0.003]*(traj_goto.shape[0]-1)+[0.0],
                    asyn=False
                    )
                time.sleep(.1)
                robot.move_joint(traj_goto[-1], asyn=False,vel=.1,)
                
                joints = robot.get_joints()
                grasp_planner.goto_joints(joints[:6]) # , show_collbodies=True)
                grasp_planner.kinematics.set_config(joints)
                # print("Computed pose from joints",grasp_planner.kinematics.ee_frame)
                # print("Pose from robot",robot.get_pose())
                # print()

                # #IK testing
                # target_pose = robot.get_pose()
                # grasp_planner.kinematics.ikmod(target_pose.matrix)
                # joints = grasp_planner.kinematics.config
                # print("Computed joints from IK",joints)
                print("Joints from robot",robot.get_joints())
                print("Target joints", traj_goto[-1,:])
                grasp_planner.goto_joints(robot.get_joints())

                if 'n' not in input("Close gripper?"):
                    robot.gripper.close()
                    time.sleep(2)
                    if 'n' not in input("Lift gripper"):
                        robot.move_joint_path(
                            traj_lift,
                            vels=[.2]*len(traj_lift),
                            accs=[1]*len(traj_lift),
                            blends = [0.01]*(len(traj_lift)-1)+[0],
                            asyn=False
                            )
                        time.sleep(0.5)

                        if primative.value == "pick & place":
                            offset_x, offset_y, offset_z = np.abs(pick_object_pt - grasp_point)
                            final_position = RigidTransform(translation=np.array([place_point[0] + offset_x, place_point[1] + offset_y, place_point[2] + offset_z]),
                                                    rotation = fin_grasp.rotation_matrix,
                                                    from_frame="grasp/ee",
                                                    to_frame="grasp/ee")
                            # import pdb; pdb.set_trace()
                            # robot.move_pose(target=final_position)
                            translation=np.array([place_point[0] + offset_x, place_point[1] + offset_y, place_point[2] + offset_z])
                            # robot.move_linear(grasp_point, translation, [0.1, 0.1, 0.1])

                        if 'n' not in input("Open gripper"):
                            robot.gripper.open()
                            time.sleep(0.5)
                            print("done")


    """
    All the functions that affect grasp generation and scores -- includes:
     - Grasp generation (for geometric scores)
     - LERF query (for semantic scores)
     - Hemisphere parameters (for generating grasps)
     - Update overall scores
     - Reset grasps

    This also includes the capability to select the best set of grasps,
    given the semantic scores and/or the geometric scores.
    """
    # Actually, prob generate the grasps when the scene is loaded.
    with server.add_gui_folder("Grasp generation"):
        with server.add_gui_folder("LERF"):
            gen_grasp_text = server.add_gui_text(
                label="LERF query",
                initial_value=""
            )
            gen_grasp_button = server.add_gui_button(
                label="Calculate LERF query",
            )
            primative = server.add_gui_dropdown(
                label="Primative",
                options = ["grasp",
                           "pick & place",
                           "twist",
                           "pour"],
                initial_value = "grasp"
            )
        with server.add_gui_folder("Grasp scores"):
            update_overall_scores_button = server.add_gui_button(
                label=f"Update overall scores",
            )
            update_overall_scores_threshold = server.add_gui_slider(
                label=f"Quantile threshold",
                min=0.0,
                max=1.0,
                initial_value=0.9,
                step=0.01,
            )
            update_overall_scores_slider = server.add_gui_slider(
                label=f"LERF score weight",
                min=0.0,
                max=1.0,
                initial_value=0.95,
                step=0.01,
            )

        """
        Updates LERF scores by updating the LERF query
        """
        @gen_grasp_button.on_click
        def _(_):
            nonlocal lerf_points_o3d, lerf_relevancy, grasps_dict, lerf_scores,ns_wrapper, pick_object_pt, place_point
            nonlocal geom_scores
            gen_grasp_text.disabled = True
            gen_grasp_button.disabled = True

            lerf_weight = update_overall_scores_slider.value
            lerf_word = gen_grasp_text.value.split(";")
            if primative.value == "pick & place" or primative.value == "pour":
                if len(lerf_word) != 3:
                    print("Please enter a valid LERF query! Expects three words")
                    gen_grasp_text.disabled = False
                    gen_grasp_button.disabled = False
                    return
            else:
                if len(lerf_word) != 2:
                    print("Please enter a valid LERF query! Expects two words")
                    gen_grasp_text.disabled = False
                    gen_grasp_button.disabled = False
                    return

            # Get the LERF activation poisntcloud for the given query
            ns_wrapper.pipeline.model.clip_scales.value = False
            ns_wrapper.pipeline.image_encoder.set_positives(lerf_word)
            lerf_points_o3d, lerf_relevancy, dino_pcd, pick_object_pt, place_point = get_relevancy_pointcloud(ns_wrapper, table_center=table_center)
            # Visualize the relevancy pointcloud 
            colors = lerf_relevancy.squeeze()
            colors = (colors - colors.min()) / (colors.max() - colors.min() + 1e-6)
            colors = matplotlib.colormaps['viridis'](colors)[:, :3]
            # server.add_point_cloud(
            #     name=f"/lerf_pointcloud_nocolor",
            #     points=np.asarray(lerf_points_o3d),
            #     colors=np.tile([0, 0, 0], (len(lerf_points_o3d), 1)),
            #     point_size=0.007,
            # )
            # if lerf_weight>0.0:
            #     server.add_point_cloud(
            #         name=f"/lerf_pointcloud",
            #         points=np.asarray(lerf_points_o3d),
            #         colors=colors,
            #         point_size=0.007,
            #     )

            lerf_scores = []
            geom_scores = []
            for i, grasp in enumerate(grasps):
                box = get_bbox_from_grasp(grasp)
                # get the indices of the lerf_xyz pointcloud which lie inside the box and avg
                pts = box.get_point_indices_within_bounding_box(lerf_points_o3d)
                pts1 = box.get_point_indices_within_bounding_box(dino_pcd)
                if len(pts) == 0:
                    score = 0.0
                else:
                    # score = lerf_relevancy[pts].mean()
                    score = np.median(lerf_relevancy[pts].squeeze()).item()
                score1 = grasp.score
                if len(pts1) == 0:
                    score1 = 0.0
                
                lerf_scores.append(score)
                geom_scores.append(score1)

            #All visualization stuff
            lerf_scores = np.array(lerf_scores)
            lerf_scores /= lerf_relevancy.max()

            gen_grasp_text.disabled = False
            gen_grasp_button.disabled = False

        """
        Update the overall scores by updating the LERF score weight
        """
        @update_overall_scores_button.on_click
        def _(_):
            nonlocal grasps_dict, overall_scores

            lerf_weight = update_overall_scores_slider.value
            geom_weight = 1.0 - lerf_weight
            # Update the scores...
            if lerf_scores is None or len(lerf_scores) == 0:
                scores = geom_scores
            elif lerf_weight == 1.0:
                scores = lerf_scores
            else:
                scores = (lerf_weight)*np.array(lerf_scores) + (geom_weight)*np.array(geom_scores)
            if len(scores) == 0:
                print("Need to run LERF query first!")
                return

            scores_threshold = np.quantile(scores, update_overall_scores_threshold.value)
            # import pdb; pdb.set_trace()
            grasps_selected = [grasp for (ind, grasp) in enumerate(grasps) if scores[ind] > scores_threshold]
            inds_selected = [ind for (ind, grasp) in enumerate(grasps) if scores[ind] > scores_threshold]

            grasps_selected = GraspGroup(np.stack([grasp.grasp_array for grasp in grasps_selected]))
            # grasps_selected = grasps_selected.nms(translation_thresh=0.02, rotation_thresh=30.0/180.0*np.pi)
            grasps_selected = grasps_selected.sort_by_score()

            for ind, grasp_list in grasps_dict.items():
                for grasp in grasp_list:
                    grasp.remove()
            grasps_dict = {}

            # min_score, max_score = scores[inds_selected].min(), scores[inds_selected].max()
            # print(scores[inds_selected].min(), scores[inds_selected].max())
            # scores -= min_score
            # scores /= max_score
            # print(scores[inds_selected].min(), scores[inds_selected].max())
            scores -= scores[inds_selected].min()
            scores /= scores[inds_selected].max()

            for ind, grasp in zip(inds_selected, grasps_selected):
                grasps_dict[ind] = add_grasps(server, grasp, ind, scores[ind])

            overall_scores = scores


    """
    All the functions that affect trajectory selection -- includes:
     - Trajectory generation
    """
    with server.add_gui_folder("Trajectory"):
        gen_traj_button = server.add_gui_button(
            label=f"Generate TRAJECTORY",
        )
        gen_traj_slider = server.add_gui_slider(
            label=f"Step",
            min=0.0,
            max=1.0,
            initial_value=0.0,
            step=0.01
        )

        @gen_traj_button.on_click
        def _(_):
            nonlocal traj, grasp_point, fin_grasp
            if grasps is None or overall_scores is None or len(grasps_dict) == 0:
                return
            
            scores = overall_scores

            gen_traj_button.disabled = True

            # traj_grasp_ind_list = np.argsort(grasps.scores)[::-1]
            traj_grasp_ind_list = np.argsort(scores)[::-1]
            for traj_grasp_ind in traj_grasp_ind_list:
                ur52grasp_pose = grasps_dict[traj_grasp_ind][-1]
                grasp2world_pose = grasps_dict[traj_grasp_ind][0]

                num_rotations_test = 1

                succ_traj_list = [] # store (traj, fin_pose)
                ur5_frame.visible = False

                start = time.time()
                print("Trying grasp", traj_grasp_ind)
                if primative.value == "grasp" or primative.value == "pick & place":
                    for i in range(num_rotations_test):
                        print("Trying rotation", i)
                        # grasp_pose = RigidTransform(
                        #     translation=grasp2world_pose.position,
                        #     rotation=tf.SO3(grasp2world_pose.wxyz).as_matrix(),
                        #     from_frame="grasp",
                        #     to_frame="world"
                        # ) * RigidTransform(
                        #     rotation=RigidTransform.y_axis_rotation(i * (2*np.pi)/num_rotations_test),
                        #     from_frame="grasp",
                        #     to_frame="grasp"
                        # ) * RigidTransform(
                        #     translation=ur52grasp_pose.position,
                        #     rotation=tf.SO3(ur52grasp_pose.wxyz).as_matrix(),
                        #     from_frame="grasp/ee",
                        #     to_frame="grasp"
                        # )
                        grasp_pose = RigidTransform(
                            translation=grasp2world_pose.position,
                            rotation=tf.SO3(grasp2world_pose.wxyz).as_matrix(),
                            from_frame="grasp",
                            to_frame="world"
                        ) * RigidTransform(
                            translation=ur52grasp_pose.position,
                            rotation=tf.SO3(ur52grasp_pose.wxyz).as_matrix(),
                            from_frame="grasp/ee",
                            to_frame="grasp"
                        ) * RigidTransform(
                            translation=np.array([0, 0, 0.02]),
                            from_frame="grasp/ee",
                            to_frame="grasp/ee"
                        ) * RigidTransform(
                            rotation=RigidTransform.x_axis_rotation(i * (2*np.pi)/num_rotations_test),
                            from_frame="grasp/ee",
                            to_frame="grasp/ee"
                        )* RigidTransform(
                            translation=np.array([0, 0, -0.02]),
                            from_frame="grasp/ee",
                            to_frame="grasp/ee"
                        )

                        if primative.value == "grasp":
                            grasp_pose.translation = [grasp_pose.translation[0], grasp_pose.translation[1], grasp_pose.translation[2] + 0.015]
            
                        if primative.value == "pick & place":
                            grasp_pose.translation = np.array([grasp_pose.translation[0] - 0.02, grasp_pose.translation[1] + 0.03, grasp_pose.translation[2]- 0.015])
                            # grasp_pose = grasp_pose * RigidTransform(
                            #     rotation=RigidTransform.y_axis_rotation(-np.pi/12),
                            #     from_frame="grasp/ee",
                            #     to_frame="grasp/ee"
                            # )


                        if grasp_pose.matrix[:, 2][2] > 0:
                            continue

                        grasp = Grasp()
                        grasp.translation = grasp_pose.translation
                        grasp.rotation_matrix = grasp_pose.rotation
                        # import pdb; pdb.set_trace()
                        box = get_bbox_from_grasp(grasp)
                        # get the indices of the lerf_xyz pointcloud which lie inside the box and avg
                        pts = box.get_point_indices_within_bounding_box(lerf_points_o3d)
                        pts = np.asarray(lerf_points_o3d)[pts]
                        center_point = pts.mean(axis=0)
                        grasp_pose = RigidTransform(translation=np.array([center_point[0] - grasp_pose.translation[0], center_point[1] - grasp_pose.translation[1], 0]),
                                                    from_frame="world",
                                                    to_frame="world") * grasp_pose
                        print("Center Point", center_point)


                        traj, succ, fin_pose = grasp_planner.create_traj_from_grasp(grasp_pose, world_pointcloud=world_pointcloud)
                        if not succ:
                            print(" - Failed")
                            continue
                        traj_up, succ_up = grasp_planner.create_traj_lift_up(
                            traj[-1, :],
                            fin_pose,
                            0.2,
                            world_pointcloud=world_pointcloud,
                        )
                        if succ and succ_up:
                            print(" - Success")
                            import pdb; pdb.set_trace()
                            succ_traj_list.append((traj, fin_pose))
                            if i == 0:
                                print(" - calculated w/o rotation")
                                break
                        else:
                            print(" - Failed")

                elif primative.value == "twist":
                    grasp_pose = RigidTransform(
                            translation=grasp2world_pose.position,
                            rotation=np.array([[-1, 0, 0], [0, 1, 0], [0, 0, -1]]),
                            from_frame="grasp",
                            to_frame="world"
                        ) * RigidTransform(
                            translation=ur52grasp_pose.position,
                            # rotation=tf.SO3(ur52grasp_pose.wxyz).as_matrix(),
                            from_frame="grasp/ee",
                            to_frame="grasp"
                        )
                    grasp = Grasp()
                    grasp.translation = grasp_pose.translation
                    grasp.rotation_matrix = grasp_pose.rotation
                    
                    box = get_bbox_from_grasp(grasp)
                    # get the indices of the lerf_xyz pointcloud which lie inside the box and avg
                    pts = box.get_point_indices_within_bounding_box(lerf_points_o3d)
                    pts = np.asarray(lerf_points_o3d)[pts]
                    center_point = pts.mean(axis=0)
                    grasp_pose = RigidTransform(
                                                translation=np.array([center_point[0] - grasp_pose.translation[0], center_point[1] - grasp_pose.translation[1], 0]),
                                                from_frame="world",
                                                to_frame="world") * grasp_pose
                    print("Center Point", center_point)

                    traj, succ, fin_pose = grasp_planner.create_traj_from_grasp(grasp_pose, world_pointcloud=world_pointcloud)
                    if not succ:
                        print(" - Failed")
                    else:
                        succ_traj_list.append((traj, fin_pose))
                elif primative.value == "pour":
                    grasp_pose = RigidTransform(
                            translation=grasp2world_pose.position,
                            rotation=np.array([[1, 0, 0], [0, 0, 1], [0, -1, 0]]),
                            from_frame="grasp",
                            to_frame="world"
                        ) * RigidTransform(
                            translation=ur52grasp_pose.position,
                            # rotation=tf.SO3(ur52grasp_pose.wxyz).as_matrix(),
                            from_frame="grasp/ee",
                            to_frame="grasp"
                        ) * RigidTransform(
                            translation=np.array([0, 0, 0]),
                            from_frame="grasp/ee",
                            to_frame="grasp/ee"
                        )
                    
                    grasp_pose.translation = [grasp_pose.translation[0], grasp_pose.translation[1]-0.03, grasp_pose.translation[2] - 0.025]

                    grasp = Grasp()
                    grasp.translation = grasp_pose.translation
                    grasp.rotation_matrix = grasp_pose.rotation
                    
                    box = get_bbox_from_grasp(grasp)
                    # get the indices of the lerf_xyz pointcloud which lie inside the box and avg
                    pts = box.get_point_indices_within_bounding_box(lerf_points_o3d)
                    pts = np.asarray(lerf_points_o3d)[pts]
                    center_point = pts.mean(axis=0)
                    grasp_pose = RigidTransform(
                                                translation=np.array([center_point[0] - grasp_pose.translation[0], center_point[1] - grasp_pose.translation[1], 0]),
                                                from_frame="world",
                                                to_frame="world") * grasp_pose
                    print("Center Point", center_point)

                    traj, succ, fin_pose = grasp_planner.create_traj_from_grasp(grasp_pose, world_pointcloud=world_pointcloud)
                    if not succ:
                        print(" - Failed")
                    else:
                        # import pdb; pdb.set_trace()                        
                        succ_traj_list.append((traj, fin_pose))

                # if not succ:
                if len(succ_traj_list) == 0:
                    print("None succeeded")
                else:
                    print("Succeeded")
                    break
                
            if len(succ_traj_list) == 0:
                print("No trajectory found")
                traj = None
                gen_traj_slider.value = 0.0
                gen_traj_button.disabled = False
                return

            min_dist = np.inf
            best_traj, best_end_pose = None, None
            for curr_traj, end_pose in succ_traj_list:
                dist = np.linalg.norm(curr_traj[0, :] - curr_traj[-1, :])
                # dist matters, but making sure that the end pose is aligned is very important.
                if dist < min_dist and ((best_end_pose is None) or (np.linalg.norm(best_end_pose.translation-grasp_pose.translation) > np.linalg.norm(end_pose.translation-grasp_pose.translation))):
                    min_dist = dist
                    best_traj = curr_traj
                    best_end_pose = end_pose

            traj, fin_pose = best_traj, best_end_pose
            succ = True

            grasp_point = fin_pose.translation
            fin_grasp = fin_pose

            if primative.value == "grasp" or primative.value == "pick & place" or primative.value == "pour":
                traj_up, succ_up = grasp_planner.create_traj_lift_up(
                    traj[-1, :],
                    fin_pose,
                    0.2,
                    world_pointcloud=world_pointcloud,
                )
                succ = succ and succ_up
            elif primative.value == "twist":
                print("Here")
                traj_up, succ_up = grasp_planner.create_traj_twist(
                    traj[-1, :],
                    fin_pose,
                    -1,
                    world_pointcloud=world_pointcloud,
                )
                succ = succ and succ_up

            if primative.value == "grasp" and not succ:
                print("Failed for the lift")
                traj = None
                grasp_planner.goto_joints(grasp_planner.UR5_HOME_JOINT)
                gen_traj_slider.value = 0.0
            else:
                print("succeeded!")
                traj = np.concatenate([traj, traj_up], axis=0)
                if primative.value == "pick & place":
                    offset_x, offset_y, offset_z = np.abs(pick_object_pt - grasp_point)
                    place_pose = RigidTransform(translation=np.array([place_point[0] + offset_x + 0.02, place_point[1] - offset_y, place_point[2] + offset_z + 0.05]),
                                            rotation = fin_grasp.rotation,
                                            from_frame="grasp/ee",
                                            to_frame="grasp/ee")
                    traj_place, succ_up = grasp_planner.create_traj_place(
                    traj[-1, :],
                    place_pose,
                    world_pointcloud=world_pointcloud,
                    )
                    traj = np.concatenate([traj, traj_place], axis=0)
                elif primative.value == "pour":
                    offset_x, offset_y, offset_z = np.abs(pick_object_pt - grasp_point)
                    offx, offy, offz = -0.05, 0.05, 0.19
                    place_pose = RigidTransform(translation=np.array([place_point[0] + offset_x + offx, place_point[1] + offset_y - 0.1 + offy, place_point[2] + offset_z + offz]),
                                            rotation = fin_grasp.rotation,
                                            from_frame="grasp/ee",
                                            to_frame="grasp/ee")
                    traj_place, succ_up = grasp_planner.create_traj_place(
                    traj[-1, :],
                    place_pose,
                    world_pointcloud=world_pointcloud,
                    )
                    traj = np.concatenate([traj, traj_place], axis=0)

                    traj_pour, succ_up = grasp_planner.create_traj_pour(
                        traj[-1, :],
                        place_pose,
                        -1.4,
                        world_pointcloud=world_pointcloud,
                    )
                    traj = np.concatenate([traj, traj_pour], axis=0)
                    
                    traj_unpour, succ_up = grasp_planner.create_traj_pour(
                        traj[-1, :],
                        place_pose,
                        0.01,
                        world_pointcloud=world_pointcloud,
                    )
                    traj = np.concatenate([traj, traj_unpour], axis=0)

                
                gen_traj_slider.value = 0.75
                fin_pose = fin_pose * RigidTransform(
                    translation=ur52grasp_pose.position,
                    rotation=tf.SO3(ur52grasp_pose.wxyz).as_matrix(),
                    from_frame="grasp/ee",
                    to_frame="grasp"
                ).inverse()
                if np.isnan(fin_pose.translation).any():
                    import pdb; pdb.set_trace()
                import pdb; pdb.set_trace()
                fin_grasp = Grasp()
                fin_grasp.translation = fin_pose.translation
                fin_grasp.rotation_matrix = fin_pose.rotation
                fin_grasp.width = grasps[int(traj_grasp_ind)].width
                fin_grasp.height = grasps[int(traj_grasp_ind)].height
                fin_grasp.depth = grasps[int(traj_grasp_ind)].depth
                print("FIN_GRASP", fin_grasp)
                add_grasps(server, fin_grasp, -1, 1.0)

            print(f"Time taken: {time.time() - start}")

            # reset ur5 pose
            ur5_frame.visible = True
            gen_traj_button.disabled = False

        """
        Update the UR5 robot vis based on the trajectory
        """
        @gen_traj_slider.on_update
        def _(_):
            if traj is None:
                return
            curr_joint = traj[int(gen_traj_slider.value*(len(traj)-1)), :]
            grasp_planner.goto_joints(curr_joint)

    with server.add_gui_folder("Recording"):
        record_path = server.add_gui_text(
                label="Output Path",
                initial_value=""
            )
        record_button = server.add_gui_button(
            label="Record",
        )

        @record_button.on_click
        def _(_):
            if record_path.value == "":
                print("Enter a path ")
                return 
            
            # import pdb; pdb.set_trace()
            with server.record(Path("viser_assets/" + record_path.value + ".viser")):
                time.sleep(0.1)


    while True:
        time.sleep(0.1)

if __name__ == "__main__":
    tyro.cli(main)

